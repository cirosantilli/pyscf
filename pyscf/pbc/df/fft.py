#!/usr/bin/env python
# Copyright 2014-2018 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

'''Density expansion on plane waves'''

import copy
import numpy
from pyscf import lib
from pyscf import gto
from pyscf.lib import logger
from pyscf.pbc import tools
from pyscf.pbc.gto import pseudo, estimate_ke_cutoff, error_for_ke_cutoff
from pyscf.pbc.df import ft_ao
from pyscf.pbc.df import fft_ao2mo
from pyscf.pbc.lib.kpts_helper import is_zero, gamma_point
from pyscf import __config__

KE_SCALING = getattr(__config__, 'pbc_df_aft_ke_cutoff_scaling', 0.75)


def get_nuc(mydf, kpts=None):
    cell = mydf.cell
    if kpts is None:
        kpts_lst = numpy.zeros((1,3))
    else:
        kpts_lst = numpy.reshape(kpts, (-1,3))

    low_dim_ft_type = mydf.low_dim_ft_type
    mesh = mydf.mesh
    charge = -cell.atom_charges()
    Gv = cell.get_Gv(mesh)
    SI = cell.get_SI(Gv)
    rhoG = numpy.dot(charge, SI)

    coulG = tools.get_coulG(cell, mesh=mesh, Gv=Gv, low_dim_ft_type=low_dim_ft_type)
    vneG = rhoG * coulG
    vneR = tools.ifft(vneG, mydf.mesh).real

    vne = [0] * len(kpts_lst)
    for ao_ks_etc, p0, p1 in mydf.aoR_loop(mydf.grids, kpts_lst):
        ao_ks = ao_ks_etc[0]
        for k, ao in enumerate(ao_ks):
            vne[k] += lib.dot(ao.T.conj()*vneR[p0:p1], ao)
        ao = ao_ks = None

    if kpts is None or numpy.shape(kpts) == (3,):
        vne = vne[0]
    return numpy.asarray(vne)

def get_pp(mydf, kpts=None):
    '''Get the periodic pseudotential nuc-el AO matrix, with G=0 removed.
    '''
    cell = mydf.cell
    if kpts is None:
        kpts_lst = numpy.zeros((1,3))
    else:
        kpts_lst = numpy.reshape(kpts, (-1,3))

    low_dim_ft_type = mydf.low_dim_ft_type
    mesh = mydf.mesh
    SI = cell.get_SI()
    Gv = cell.get_Gv(mesh)
    vpplocG = pseudo.get_vlocG(cell, Gv, low_dim_ft_type)
    vpplocG = -numpy.einsum('ij,ij->j', SI, vpplocG)
    # from get_jvloc_G0 function
    vpplocG[0] = numpy.sum(pseudo.get_alphas(cell, low_dim_ft_type))
    ngrids = len(vpplocG)

    # vpploc evaluated in real-space
    vpplocR = tools.ifft(vpplocG, mesh).real
    vpp = [0] * len(kpts_lst)
    for ao_ks_etc, p0, p1 in mydf.aoR_loop(mydf.grids, kpts_lst):
        ao_ks = ao_ks_etc[0]
        for k, ao in enumerate(ao_ks):
            vpp[k] += lib.dot(ao.T.conj()*vpplocR[p0:p1], ao)
        ao = ao_ks = None

    # vppnonloc evaluated in reciprocal space
    fakemol = gto.Mole()
    fakemol._atm = numpy.zeros((1,gto.ATM_SLOTS), dtype=numpy.int32)
    fakemol._bas = numpy.zeros((1,gto.BAS_SLOTS), dtype=numpy.int32)
    ptr = gto.PTR_ENV_START
    fakemol._env = numpy.zeros(ptr+10)
    fakemol._bas[0,gto.NPRIM_OF ] = 1
    fakemol._bas[0,gto.NCTR_OF  ] = 1
    fakemol._bas[0,gto.PTR_EXP  ] = ptr+3
    fakemol._bas[0,gto.PTR_COEFF] = ptr+4

    # buf for SPG_lmi upto l=0..3 and nl=3
    buf = numpy.empty((48,ngrids), dtype=numpy.complex128)
    def vppnl_by_k(kpt):
        Gk = Gv + kpt
        G_rad = lib.norm(Gk, axis=1)
        aokG = ft_ao.ft_ao(cell, Gv, kpt=kpt) * (ngrids/cell.vol)
        vppnl = 0
        for ia in range(cell.natm):
            symb = cell.atom_symbol(ia)
            if symb not in cell._pseudo:
                continue
            pp = cell._pseudo[symb]
            p1 = 0
            for l, proj in enumerate(pp[5:]):
                rl, nl, hl = proj
                if nl > 0:
                    fakemol._bas[0,gto.ANG_OF] = l
                    fakemol._env[ptr+3] = .5*rl**2
                    fakemol._env[ptr+4] = rl**(l+1.5)*numpy.pi**1.25
                    pYlm_part = fakemol.eval_gto('GTOval', Gk)

                    p0, p1 = p1, p1+nl*(l*2+1)
                    # pYlm is real, SI[ia] is complex
                    pYlm = numpy.ndarray((nl,l*2+1,ngrids), dtype=numpy.complex128, buffer=buf[p0:p1])
                    for k in range(nl):
                        qkl = pseudo.pp._qli(G_rad*rl, l, k)
                        pYlm[k] = pYlm_part.T * qkl
                    #:SPG_lmi = numpy.einsum('g,nmg->nmg', SI[ia].conj(), pYlm)
                    #:SPG_lm_aoG = numpy.einsum('nmg,gp->nmp', SPG_lmi, aokG)
                    #:tmp = numpy.einsum('ij,jmp->imp', hl, SPG_lm_aoG)
                    #:vppnl += numpy.einsum('imp,imq->pq', SPG_lm_aoG.conj(), tmp)
            if p1 > 0:
                SPG_lmi = buf[:p1]
                SPG_lmi *= SI[ia].conj()
                SPG_lm_aoGs = lib.zdot(SPG_lmi, aokG)
                p1 = 0
                for l, proj in enumerate(pp[5:]):
                    rl, nl, hl = proj
                    if nl > 0:
                        p0, p1 = p1, p1+nl*(l*2+1)
                        hl = numpy.asarray(hl)
                        SPG_lm_aoG = SPG_lm_aoGs[p0:p1].reshape(nl,l*2+1,-1)
                        tmp = numpy.einsum('ij,jmp->imp', hl, SPG_lm_aoG)
                        vppnl += numpy.einsum('imp,imq->pq', SPG_lm_aoG.conj(), tmp)
        return vppnl * (1./ngrids**2)

    for k, kpt in enumerate(kpts_lst):
        vppnl = vppnl_by_k(kpt)
        if gamma_point(kpt):
            vpp[k] = vpp[k].real + vppnl.real
        else:
            vpp[k] += vppnl

    if kpts is None or numpy.shape(kpts) == (3,):
        vpp = vpp[0]
    return numpy.asarray(vpp)


class FFTDF(lib.StreamObject):
    '''Density expansion on plane waves
    '''
    def __init__(self, cell, kpts=numpy.zeros((1,3))):
        from pyscf.pbc.dft import gen_grid
        from pyscf.pbc.dft import numint
        self.cell = cell
        self.stdout = cell.stdout
        self.verbose = cell.verbose
        self.max_memory = cell.max_memory
        self.low_dim_ft_type = cell.low_dim_ft_type

        self.kpts = kpts
        self.grids = gen_grid.UniformGrids(cell)

        # to mimic molecular DF object
        self.blockdim = getattr(__config__, 'pbc_df_df_DF_blockdim', 240)

# Not input options
        self.exxdiv = None  # to mimic KRHF/KUHF object in function get_coulG
        self._numint = numint._KNumInt()
        self._keys = set(self.__dict__.keys())

    @property
    def mesh(self):
        return self.grids.mesh
    @mesh.setter
    def mesh(self, mesh):
        self.grids.mesh = mesh

    def dump_flags(self):
        logger.info(self, '\n')
        logger.info(self, '******** %s flags ********', self.__class__)
        logger.info(self, 'mesh = %s (%d PWs)', self.mesh, numpy.prod(self.mesh))
        logger.info(self, 'len(kpts) = %d', len(self.kpts))
        logger.debug1(self, '    kpts = %s', self.kpts)
        return self

    def check_sanity(self):
        lib.StreamObject.check_sanity(self)
        cell = self.cell
        if cell.dimension < 2:
            raise RuntimeError('FFTDF method does not support 0D/1D low-dimension '
                               'PBC system.  DF, MDF or AFTDF methods should '
                               'be used.\nSee also examples/pbc/31-low_dimensional_pbc.py')
        if cell.dimension == 2 and self.low_dim_ft_type is None:
            raise RuntimeError('FFTDF method does not support low_dim_ft_type of None '
                               'for 2D systems.  Supported types include \'analytic_2d_1\'. '
                               '\nSee also examples/pbc/32-graphene.py')

        if not cell.has_ecp():
            logger.warn(self, 'FFTDF integrals are found in all-electron '
                        'calculation.  It often causes huge error.\n'
                        'Recommended methods are DF or MDF. In SCF calculation, '
                        'they can be initialized as\n'
                        '        mf = mf.density_fit()\nor\n'
                        '        mf = mf.mix_density_fit()')

        if cell.ke_cutoff is None:
            ke_cutoff = tools.mesh_to_cutoff(cell.lattice_vectors(), self.mesh).min()
        else:
            ke_cutoff = numpy.min(cell.ke_cutoff)
        ke_guess = estimate_ke_cutoff(cell, cell.precision)
        if ke_cutoff < ke_guess * KE_SCALING:
            mesh_guess = tools.cutoff_to_mesh(cell.lattice_vectors(), ke_guess)
            logger.warn(self, 'ke_cutoff/mesh (%g / %s) is not enough for FFTDF '
                        'to get integral accuracy %g.\nCoulomb integral error '
                        'is ~ %.2g Eh.\nRecommended ke_cutoff/mesh are %g / %s.',
                        ke_cutoff, self.mesh, cell.precision,
                        error_for_ke_cutoff(cell, ke_cutoff), ke_guess, mesh_guess)
        return self

    def aoR_loop(self, grids=None, kpts=None, deriv=0):
        if grids is None:
            grids = self.grids
            cell = self.cell
        else:
            cell = grids.cell
        if grids.non0tab is None:
            grids.build(with_non0tab=True)

        if kpts is None: kpts = self.kpts
        kpts = numpy.asarray(kpts)

        if cell.dimension < 2:
            raise RuntimeError('FFTDF method does not support low-dimension '
                               'PBC system.  DF, MDF or AFTDF methods should '
                               'be used.\nSee also examples/pbc/31-low_dimensional_pbc.py')
        if cell.dimension == 2 and self.low_dim_ft_type is None:
            raise RuntimeError('FFTDF method only supports low_dim_ft_type of None '
                               'for 2D systems.  Supported types include \'analytic_2d_1\'. '
                               '\nSee also examples/pbc/32-graphene.py')

        max_memory = max(2000, self.max_memory-lib.current_memory()[0])
        ni = self._numint
        nao = cell.nao_nr()
        p1 = 0
        for ao_k1_etc in ni.block_loop(cell, grids, nao, deriv, kpts,
                                       max_memory=max_memory):
            coords = ao_k1_etc[4]
            p0, p1 = p1, p1 + coords.shape[0]
            yield ao_k1_etc, p0, p1

    get_pp = get_pp
    get_nuc = get_nuc

    def get_jk(self, dm, hermi=1, kpts=None, kpts_band=None,
               with_j=True, with_k=True, exxdiv='ewald'):
        from pyscf.pbc.df import fft_jk
        if kpts is None:
            if numpy.all(self.kpts == 0):
                # Gamma-point calculation by default
                kpts = numpy.zeros(3)
            else:
                kpts = self.kpts
        else:
            kpts = numpy.asarray(kpts)

        vj = vk = None
        if kpts.shape == (3,):
            vj, vk = fft_jk.get_jk(self, dm, hermi, kpts, kpts_band,
                                   with_j, with_k, exxdiv)
        else:
            if with_k:
                vk = fft_jk.get_k_kpts(self, dm, hermi, kpts, kpts_band, exxdiv)
            if with_j:
                vj = fft_jk.get_j_kpts(self, dm, hermi, kpts, kpts_band)
        return vj, vk

    get_eri = get_ao_eri = fft_ao2mo.get_eri
    ao2mo = get_mo_eri = fft_ao2mo.general
    get_ao_pairs_G = get_ao_pairs = fft_ao2mo.get_ao_pairs_G
    get_mo_pairs_G = get_mo_pairs = fft_ao2mo.get_mo_pairs_G

    def update_mf(self, mf):
        mf = copy.copy(mf)
        mf.with_df = self
        return mf

################################################################################
# With this function to mimic the molecular DF.loop function, the pbc gamma
# point DF object can be used in the molecular code
    def loop(self, blksize=None):
        if blksize is None:
            blksize = self.blockdim
        kpts0 = numpy.zeros((2,3))
        coulG = tools.get_coulG(self.cell, numpy.zeros(3), mesh=self.mesh,
                                low_dim_ft_type=self.low_dim_ft_type)
        ngrids = len(coulG)
        ao_pairs_G = self.get_ao_pairs_G(kpts0, compact=True)
        ao_pairs_G *= numpy.sqrt(coulG*(self.cell.vol/ngrids**2)).reshape(-1,1)

        Lpq = numpy.empty((self.blockdim, ao_pairs_G.shape[1]))
        for p0, p1 in lib.prange(0, ngrids, blksize):
            Lpq[:p1-p0] = ao_pairs_G[p0:p1].real
            yield Lpq[:p1-p0]
            Lpq[:p1-p0] = ao_pairs_G[p0:p1].imag
            yield Lpq[:p1-p0]

    def get_naoaux(self):
        mesh = numpy.asarray(self.mesh)
        ngrids = numpy.prod(mesh)
        return ngrids * 2


if __name__ == '__main__':
    from pyscf.pbc import gto as pbcgto
    cell = pbcgto.Cell()
    cell.verbose = 0
    cell.atom = 'C 0 0 0; C 1 1 1; C 0 2 2; C 2 0 2'
    cell.a = numpy.diag([4, 4, 4])
    cell.basis = 'gth-szv'
    cell.pseudo = 'gth-pade'
    cell.mesh = [20]*3
    cell.build()
    k = numpy.ones(3)*.25
    df = FFTDF(cell)
    v1 = get_pp(df, k)
    print(lib.finger(v1) - (1.8428463642697195-0.10478381725330854j))
    v1 = get_nuc(df, k)
    print(lib.finger(v1) - (2.3454744614944714-0.12528407127454744j))


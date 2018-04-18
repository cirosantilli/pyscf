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

'''
Non-relativistic Hartree-Fock analytical nuclear gradients
'''

import time
import numpy
from pyscf import lib
from pyscf.lib import logger
from pyscf.scf import _vhf


def grad_elec(mf_grad, mo_energy=None, mo_coeff=None, mo_occ=None, atmlst=None):
    mf = mf_grad.base
    mol = mf_grad.mol
    if mo_energy is None: mo_energy = mf.mo_energy
    if mo_occ is None:    mo_occ = mf.mo_occ
    if mo_coeff is None:  mo_coeff = mf.mo_coeff
    log = logger.Logger(mf_grad.stdout, mf_grad.verbose)

    hcore_deriv = mf_grad.hcore_generator(mol)
    s1 = mf_grad.get_ovlp(mol)
    dm0 = mf.make_rdm1(mo_coeff, mo_occ)

    t0 = (time.clock(), time.time())
    log.debug('Computing Gradients of NR-HF Coulomb repulsion')
    vhf = mf_grad.get_veff(mol, dm0)
    log.timer('gradients of 2e part', *t0)

    dme0 = mf_grad.make_rdm1e(mo_energy, mo_coeff, mo_occ)

    if atmlst is None:
        atmlst = range(mol.natm)
    aoslices = mol.aoslice_by_atom()
    de = numpy.zeros((len(atmlst),3))
    for k, ia in enumerate(atmlst):
        shl0, shl1, p0, p1 = aoslices[ia]
        h1ao = hcore_deriv(ia)
        de[k] += numpy.einsum('xij,ij->x', h1ao, dm0)
# nabla was applied on bra in vhf, *2 for the contributions of nabla|ket>
        de[k] += numpy.einsum('xij,ij->x', vhf[:,p0:p1], dm0[p0:p1]) * 2
        de[k] -= numpy.einsum('xij,ij->x', s1[:,p0:p1], dme0[p0:p1]) * 2
        if mf_grad.grid_response: # Only effective in DFT gradients
            de[k] += vhf.exc1_grid[ia]
    if log.verbose >= logger.DEBUG:
        log.debug('gradients of electronic part')
        _write(log, mol, de, atmlst)
        if mf_grad.grid_response:
            log.debug('grids response contributions')
            _write(log, mol, vhf.exc1_grid[atmlst], atmlst)
            log.debug1('sum(de) %s', vhf.exc1_grid.sum(axis=0))
    return de

def _write(dev, mol, de, atmlst):
    dev.stdout.write('         x                y                z\n')
    for k, ia in enumerate(atmlst):
        dev.stdout.write('%d %s  %15.10f  %15.10f  %15.10f\n' %
                         (ia, mol.atom_symbol(ia), de[k,0], de[k,1], de[k,2]))


def grad_nuc(mol, atmlst=None):
    gs = numpy.zeros((mol.natm,3))
    for j in range(mol.natm):
        q2 = mol.atom_charge(j)
        r2 = mol.atom_coord(j)
        for i in range(mol.natm):
            if i != j:
                q1 = mol.atom_charge(i)
                r1 = mol.atom_coord(i)
                r = numpy.sqrt(numpy.dot(r1-r2,r1-r2))
                gs[j] -= q1 * q2 * (r2-r1) / r**3
    if atmlst is not None:
        gs = gs[atmlst]
    return gs


def get_hcore(mol):
    '''Part of the nuclear gradients of core Hamiltonian'''
    h =(mol.intor('int1e_ipkin', comp=3)
      + mol.intor('int1e_ipnuc', comp=3))
    if mol.has_ecp():
        h += mol.intor('ECPscalar_ipnuc', comp=3)
    return -h

def get_ovlp(mol):
    return -mol.intor('int1e_ipovlp', comp=3)

def get_jk(mol, dm):
    '''J = ((-nabla i) j| kl) D_lk
    K = ((-nabla i) j| kl) D_jk
    '''
    intor = mol._add_suffix('int2e_ip1')
    vj, vk = _vhf.direct_mapdm(intor,  # (nabla i,j|k,l)
                               's2kl', # ip1_sph has k>=l,
                               ('lk->s1ij', 'jk->s1il'),
                               dm, 3, # xyz, 3 components
                               mol._atm, mol._bas, mol._env)
    return -vj, -vk

def get_veff(mf_grad, mol, dm):
    '''NR Hartree-Fock Coulomb repulsion'''
    vj, vk = mf_grad.get_jk(mol, dm)
    return vj - vk * .5

def make_rdm1e(mo_energy, mo_coeff, mo_occ):
    '''Energy weighted density matrix'''
    mo0 = mo_coeff[:,mo_occ>0]
    mo0e = mo0 * (mo_energy[mo_occ>0] * mo_occ[mo_occ>0])
    return numpy.dot(mo0e, mo0.T.conj())


def as_scanner(mf_grad):
    '''Generating a nuclear gradients scanner/solver (for geometry optimizer).

    The returned solver is a function. This function requires one argument
    "mol" as input and returns energy and first order nuclear derivatives.

    The solver will automatically use the results of last calculation as the
    initial guess of the new calculation.  All parameters assigned in the
    nuc-grad object and SCF object (DIIS, conv_tol, max_memory etc) are
    automatically applied in the solver.

    Note scanner has side effects.  It may change many underlying objects
    (_scf, with_df, with_x2c, ...) during calculation.

    Examples::

        >>> from pyscf import gto, scf, grad
        >>> mol = gto.M(atom='H 0 0 0; F 0 0 1')
        >>> hf_scanner = scf.RHF(mol).apply(grad.RHF).as_scanner()
        >>> e_tot, grad = hf_scanner(gto.M(atom='H 0 0 0; F 0 0 1.1'))
        >>> e_tot, grad = hf_scanner(gto.M(atom='H 0 0 0; F 0 0 1.5'))
    '''
    logger.info(mf_grad, 'Create scanner for %s', mf_grad.__class__)
    class SCF_GradScanner(mf_grad.__class__, lib.GradScanner):
        def __init__(self, g):
            lib.GradScanner.__init__(self, g)
        def __call__(self, mol, **kwargs):
            mf_scanner = self.base
            e_tot = mf_scanner(mol)
            self.mol = mol
            de = self.kernel(**kwargs)
            return e_tot, de
    return SCF_GradScanner(mf_grad)


class Gradients(lib.StreamObject):
    '''Non-relativistic restricted Hartree-Fock gradients'''
    def __init__(self, scf_method):
        self.verbose = scf_method.verbose
        self.stdout = scf_method.stdout
        self.mol = scf_method.mol
        self.base = scf_method
        self.max_memory = self.mol.max_memory
# This parameter has no effects for HF gradients. Add this attribute so that
# the kernel function can be reused in the DFT gradients code.
        self.grid_response = False

        self.atmlst = range(self.mol.natm)
        self.de = numpy.zeros((0,3))
        self._keys = set(self.__dict__.keys())

    def dump_flags(self):
        log = logger.Logger(self.stdout, self.verbose)
        log.info('\n')
        if not self.base.converged:
            log.warn('Ground state SCF not converged')
        log.info('******** %s for %s ********',
                 self.__class__, self.base.__class__)
        log.info('max_memory %d MB (current use %d MB)',
                 self.max_memory, lib.current_memory()[0])
        return self

    def get_hcore(self, mol=None):
        if mol is None: mol = self.mol
        return get_hcore(mol)

    def hcore_generator(self, mol=None):
        if mol is None: mol = self.mol
        with_x2c = getattr(self.base, 'with_x2c', None)
        if with_x2c:
            hcore_deriv = with_x2c.hcore_deriv_generator(deriv=1)
        else:
            with_ecp = mol.has_ecp()
            aoslices = mol.aoslice_by_atom()
            h1 = self.get_hcore(mol)
            def hcore_deriv(atm_id):
                shl0, shl1, p0, p1 = aoslices[atm_id]
                with mol.with_rinv_as_nucleus(atm_id):
                    vrinv = mol.intor('int1e_iprinv', comp=3) # <\nabla|1/r|>
                    if with_ecp:
                        vrinv += mol.intor('ECPscalar_iprinv', comp=3)
                    vrinv *= -mol.atom_charge(atm_id)
                vrinv[:,p0:p1] += h1[:,p0:p1]
                return vrinv + vrinv.transpose(0,2,1)
        return hcore_deriv

    def get_ovlp(self, mol=None):
        if mol is None: mol = self.mol
        return get_ovlp(mol)

    @lib.with_doc(get_jk.__doc__)
    def get_jk(self, mol=None, dm=None, hermi=0):
        if mol is None: mol = self.mol
        if dm is None: dm = self.base.make_rdm1()
        cpu0 = (time.clock(), time.time())
        #TODO: direct_scf opt
        vj, vk = get_jk(mol, dm)
        logger.timer(self, 'vj and vk', *cpu0)
        return vj, vk

    def get_j(self, mol=None, dm=None, hermi=0):
        if mol is None: mol = self.mol
        if dm is None: dm = self.base.make_rdm1()
        intor = mol._add_suffix('int2e_ip1')
        return -_vhf.direct_mapdm(intor, 's2kl', 'lk->s1ij', dm, 3,
                                  mol._atm, mol._bas, mol._env)

    def get_k(self, mol=None, dm=None, hermi=0):
        if mol is None: mol = self.mol
        if dm is None: dm = self.base.make_rdm1()
        intor = mol._add_suffix('int2e_ip1')
        return -_vhf.direct_mapdm(intor, 's2kl', 'jk->s1il', dm, 3,
                                  mol._atm, mol._bas, mol._env)

    def get_veff(self, mol=None, dm=None):
        if mol is None: mol = self.mol
        if dm is None: dm = self.base.make_rdm1()
        return get_veff(self, mol, dm)

    def make_rdm1e(self, mo_energy=None, mo_coeff=None, mo_occ=None):
        if mo_energy is None: mo_energy = self.base.mo_energy
        if mo_coeff is None: mo_coeff = self.base.mo_coeff
        if mo_occ is None: mo_occ = self.base.mo_occ
        return make_rdm1e(mo_energy, mo_coeff, mo_occ)

    grad_elec = grad_elec

    def grad_nuc(self, mol=None, atmlst=None):
        if mol is None: mol = self.mol
        return grad_nuc(mol, atmlst)

    def grad(self, mo_energy=None, mo_coeff=None, mo_occ=None, atmlst=None):
        return self.kernel(mo_energy, mo_coeff, mo_occ, atmlst)
    def kernel(self, mo_energy=None, mo_coeff=None, mo_occ=None, atmlst=None):
        cput0 = (time.clock(), time.time())
        if mo_energy is None: mo_energy = self.base.mo_energy
        if mo_coeff is None: mo_coeff = self.base.mo_coeff
        if mo_occ is None: mo_occ = self.base.mo_occ
        if atmlst is None:
            atmlst = self.atmlst
        else:
            self.atmlst = atmlst

        if self.verbose >= logger.WARN:
            self.check_sanity()
        if self.verbose >= logger.INFO:
            self.dump_flags()

        de = self.grad_elec(mo_energy, mo_coeff, mo_occ, atmlst)
        self.de = de + self.grad_nuc(atmlst=atmlst)
        logger.timer(self, 'SCF gradients', *cput0)
        self._finalize()
        return self.de

    def _finalize(self):
        if self.verbose >= logger.NOTE:
            logger.note(self, '--------------- %s gradients ---------------',
                        self.base.__class__.__name__)
            _write(self, self.mol, self.de, self.atmlst)
            logger.note(self, '----------------------------------------------')

    as_scanner = as_scanner

Grad = Gradients


if __name__ == '__main__':
    from pyscf import gto
    from pyscf import scf
    mol = gto.Mole()
    mol.verbose = 0
    mol.atom = [['He', (0.,0.,0.)], ]
    mol.basis = {'He': 'ccpvdz'}
    mol.build()
    method = scf.RHF(mol)
    method.scf()
    g = Gradients(method)
    print(g.grad())

    h2o = gto.Mole()
    h2o.verbose = 0
    h2o.atom = [
        ['O' , (0. , 0.     , 0.)],
        [1   , (0. , -0.757 , 0.587)],
        [1   , (0. , 0.757  , 0.587)] ]
    h2o.basis = {'H': '631g',
                 'O': '631g',}
    h2o.build()
    rhf = scf.RHF(h2o)
    rhf.conv_tol = 1e-14
    e0 = rhf.scf()
    g = Gradients(rhf)
    print(g.grad())
#[[ 0   0               -2.41134256e-02]
# [ 0   4.39690522e-03   1.20567128e-02]
# [ 0  -4.39690522e-03   1.20567128e-02]]

    rhf = scf.RHF(h2o).x2c()
    rhf.conv_tol = 1e-14
    e0 = rhf.scf()
    g = Gradients(rhf)
    print(g.grad())
#[[ 0   0               -2.40286232e-02]
# [ 0   4.27908498e-03   1.20143116e-02]
# [ 0  -4.27908498e-03   1.20143116e-02]]

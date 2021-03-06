# -*- coding: utf-8 -*-
#
#   pycheops - Tools for the analysis of data from the ESA CHEOPS mission
#
#   Copyright (C) 2018  Dr Pierre Maxted, Keele University
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""
models
======
Models and likelihood functions for use with lmfit

"""

from __future__ import (absolute_import, division, print_function,
                                unicode_literals)
import numpy as np
from lmfit.model import Model
from lmfit.models import COMMON_INIT_DOC, COMMON_GUESS_DOC
from numba import jit
from .funcs import t2z, xyz_planet, vrad
from warnings import warn
from scipy.optimize import brent, brentq
from collections import OrderedDict
from asteval import Interpreter, get_ast_names, valid_symbol_name

__all__ = ['qpower2', 'ueclipse', 'TransitModel', 'EclipseModel', 
           'FactorModel', 'ThermalPhaseModel', 'ReflectionModel',
           'RVModel', 'RVCompanion', 
           'scaled_transit_fit', 'minerr_transit_fit']

@jit(nopython=True)
def qpower2(z,k,c,a):
    r"""
    Fast and accurate transit light curves for the power-2 limb-darkening law

    The power-2 limb-darkening law is I(\mu) = 1 - c (1 - \mu^\alpha)

    Light curves are calculated using the qpower2 approximation [1]. The
    approximation is accurate to better than 100ppm for radius ratio k < 0.1.

    N.B. qpower2 is untested/inaccurate for values of k > 0.2

    .. [1] Maxted, P.F.L. & Gill, S., 2019, accepted for publication in A&A.

    :param z: star-planet separation on the sky cf. star radius (array)
    :param k: planet-star radius ratio (scalar, k<1) 
    :param c: power-2 limb darkening coefficient
    :param a: power-2 limb darkening exponent

    :returns: light curve (observed flux)  

    :Example:
    >>> from pycheops.models import qpower2
    >>> from pycheops.funcs import t2z
    >>> from numpy import linspace
    >>> import matplotlib.pyplot as plt
    >>> t = linspace(-0.025,0.025,1000)
    >>> sini = 0.999
    >>> rstar = 0.05
    >>> ecc = 0.2
    >>> om = 120
    >>> tzero = 0.0
    >>> P = 0.1
    >>> z=t2z(t,tzero,P,sini,rstar,ecc,om)
    >>> c = 0.5
    >>> a = 0.7
    >>> k = 0.1
    >>> f = qpower2(z,k,c,a)
    >>> plt.plot(t,f)
    >>> plt.show()

    """
    f = np.ones_like(z)
    I_0 = (a+2)/(np.pi*(a-c*a+2))
    g = 0.5*a
    for i,zi in enumerate(z):
        zt = np.abs(zi)
        if zt <= (1-k):
            s = 1-zt**2
            c0 = (1-c+c*s**g)
            c2 = 0.5*a*c*s**(g-2)*((a-1)*zt**2-1)
            f[i] = 1-I_0*np.pi*k**2*(
                    c0 + 0.25*k**2*c2 - 0.125*a*c*k**2*s**(g-1) )
        elif np.abs(zt-1) < k:
            d = (zt**2 - k**2 + 1)/(2*zt)
            ra = 0.5*(zt-k+d)
            rb = 0.5*(1+d)
            sa = 1-ra**2
            sb = 1-rb**2
            q = min(max(-1.,(zt-d)/k),1.)
            w2 = k**2-(d-zt)**2
            w = np.sqrt(w2)
            b0 = 1 - c + c*sa**g
            b1 = -a*c*ra*sa**(g-1)
            b2 = 0.5*a*c*sa**(g-2)*((a-1)*ra**2-1)
            a0 = b0 + b1*(zt-ra) + b2*(zt-ra)**2
            a1 = b1+2*b2*(zt-ra)
            aq = np.arccos(q)
            J1 = ( (a0*(d-zt)-(2/3)*a1*w2 + 
                0.25*b2*(d-zt)*(2*(d-zt)**2-k**2))*w
                 + (a0*k**2 + 0.25*b2*k**4)*aq )
            J2 = a*c*sa**(g-1)*k**4*(
                0.125*aq + (1/12)*q*(q**2-2.5)*np.sqrt(max(0.,1-q**2)) )
            d0 = 1 - c + c*sb**g
            d1 = -a*c*rb*sb**(g-1)
            K1 = ((d0-rb*d1)*np.arccos(d) + 
                    ((rb*d+(2/3)*(1-d**2))*d1 - d*d0) * 
                    np.sqrt(max(0.,1-d**2)) )
            K2 = (1/3)*c*a*sb**(g+0.5)*(1-d)
            f[i] = 1 - I_0*(J1 - J2 + K1 - K2)
    return f

@jit(nopython=True)
def scaled_transit_fit(flux, sigma, model):
    """
    Optimum scaled transit depth for data with scaled errors

    Find the value of the scaling factor s that provides the best fit of the
    model m = 1 + s*(model-1) to the normalised input fluxes. It is assumed
    that the true standard errors on the flux measurements are a factor f
    times the nominal standard error(s) provided in sigma. Also returns
    standard error estimates for s and f, sigma_s and sigma_f, respectively.

     :param flux: Array of normalised flux measurements

     :param sigma: Standard error estimate(s) for flux - array or scalar

     :param model: Transit model to be scaled

     :returns: s, b, sigma_s, sigma_b

    """
    N = len(flux)
    if N < 3:
        return np.nan, np.nan, np.nan, np.nan

    w = 1/sigma**2
    _m = np.sum(w*(model-1)**2)
    if _m == 0:
        return np.nan, np.nan, np.nan, np.nan
    s = np.sum(w*(model-1)*(flux-1))/_m
    chisq = np.sum(w*((flux-1)-s*(model-1))**2)
    b = np.sqrt(chisq/N)
    sigma_s = b/np.sqrt(_m)
    _t = 3*chisq/b**4 - N/b**2 
    if _t > 0:
        sigma_b = 1/np.sqrt(_t)
    else:
        return np.nan, np.nan, np.nan, np.nan
    return s, b, sigma_s, sigma_b


def minerr_transit_fit(flux, sigma, model):
    """
    Optimum scaled transit depth for data with lower bounds on errors

    Find the value of the scaling factor s that provides the best fit of the
    model m = 1 + s*(model-1) to the normalised input fluxes. It is assumed
    that the nominal standard error(s) provided in sigma are lower bounds to
    the true standard errors on the flux measurements. The probability
    distribution for the true standard errors is assumed to be [1]
        P(sigma_true|sigma) = sigma/sigma_true**2


     :param flux: Array of normalised flux measurements

     :param sigma: Lower bound(s) on standard error for flux - array or scalar

     :param model: Transit model to be scaled

     :returns: s, sigma_s

  
.. rubric References
.. [1] Sivia, D.S. & Skilling, J., Data Analysis - A Bayesian Tutorial, 2nd
   ed., section 8.3.1

    """
    N = len(flux)
    if N < 3:
        return np.nan, np.nan

    def _negloglike(s, flux, sigma, model):
        model =  1 + s*(model-1)
        Rsq = ((model-flux)/sigma)**2
        # In the limit Rsq -> 0, log-likelihood -> log(0.5)
        x = np.full_like(Rsq,np.log(0.5))
        _j = Rsq > np.finfo(0.0).eps
        x[_j] = np.log((1-np.exp(-0.5*Rsq[_j]))/Rsq[_j])
        return -np.sum(x)

    def _loglikediff(s, loglike_0, flux, sigma, model):
        return loglike_0 + _negloglike(s, flux, sigma, model)

    if np.min(model) == 1:
        return 0,0
    # Bracket the minimum of _negloglike
    s_min = 0
    fa = _negloglike(s_min, flux, sigma, model)
    s_mid = 1
    fb = _negloglike(s_mid, flux, sigma, model)
    #print('s_min, fa, s_mid, fb',s_min, fa, s_mid, fb)
    if fb < fa:
        s_max = 2
        fc = _negloglike(s_max, flux, sigma, model)
        while fc < fb:
            s_max = 2*s_max
            fc = _negloglike(s_max, flux, sigma, model)
    else:
        s_max = s_mid
        fc = fb
        s_mid = 0.5
        fb = _negloglike(s_mid, flux, sigma, model)
        while fb > fa:
            if s_mid < 2**-16:
                return 0,0
            s_mid = 0.5*s_mid
            fb = _negloglike(s_mid, flux, sigma, model)

    #print('s_min, fa, s_mid, fb, s_max, fc',s_min, fa, s_mid, fb, s_max, fc)
    s_opt, _f, _, _ = brent(_negloglike, args=(flux, sigma, model),
                       brack=(s_min,s_mid,s_max), full_output=True)
    loglike_0 = -_f -0.5
    s_hi = s_max
    f_hi = _loglikediff(s_hi, loglike_0, flux, sigma, model)
    while f_hi < 0:
        s_hi = 2*s_hi
        f_hi = _loglikediff(s_hi, loglike_0, flux, sigma, model)
    s_hi = brentq(_loglikediff, s_opt, s_hi,
                 args = (loglike_0, flux, sigma, model))
    s_err = s_hi - s_opt
    #print('s_opt,  s_err',s_opt, s_err)
    return s_opt, s_err

@jit(nopython=True)
def ueclipse(z,k):
    """
    Eclipse light curve for a planet with uniform surface brightness by a star

    :param z: star-planet separation on the sky cf. star radius (array)
    :param k: planet-star radius ratio (scalar, k<1) 

    :returns: light curve (observed flux from eclipsed source)  
    """
    if (k > 1):
        raise ValueError("ueclipse requires k < 1")

    fl = np.ones_like(z)
    for i,zi in enumerate(z):
        zt = np.abs(zi)
        if zt <= (1-k):
            fl[i] = 0
        elif np.abs(zt-1) < k:
            t1 = np.arccos(min(max(-1,(zt**2+k**2-1)/(2*zt*k)),1))
            t2 = np.arccos(min(max(-1,(zt**2+1-k**2)/(2*zt)),1))
            t3 = 0.5*np.sqrt(max(0,(1+k-zt)*(zt+k-1)*(zt-k+1)*(zt+k+1)))
            fl[i] = 1 - (k**2*t1 + t2 - t3)/(np.pi*k**2)
    return fl

class TransitModel(Model):
    r"""Light curve model for the transit of a spherical star by an opaque
    spherical body (planet).

    Limb-darkening is described by the power-2 law:
    .. math::
        I(\mu; c, \alpha) = 1 - c (1 - \mu^\alpha)

    The light curve depth, width and shape are parameterised by D, W, and S as
    defined below in terms of the star and planet radii, R_s and R_p,
    respectively, the semi-major axis, a, and the orbital inclination, i. The
    following parameters are used for convenience - k = R_p/R_s, aR = a/R_s,
    b=aR.cos(i). The shape parameter is approximately (t_F/t_T)^2 where
    t_T=W*P is the duration of the transit (1st to 4th contact points) and t_F
    is the duration of the "flat" part of the transit between the 2nd and 3rd
    contact points. These parameters are all available as constraints within
    the model. Also available is the mean stellar density in solar units,
    rho=0.013418*aR^3/(P/days)^2. N.B. this value of rho assumes that
    M_planet << M_star. The eccentricity and longitude of periastron for the
    planet's orbit are ecc and omega, respectively.

    :param t:    - independent variable (time)
    :param T_0:  - time of mid-transit
    :param P:    - orbital period
    :param D:    - (R_p/R_s)^2 = k^2
    :param W:    - (R_s/a)*sqrt((1+k)^2 - b^2)/pi
    :param S:    - ((1-k)^2-b^2)/((1+k)^2 - b^2)
    :param f_c:  - sqrt(ecc).cos(omega)
    :param f_s:  - sqrt(ecc).sin(omega)
    :param h_1:  - I(0.5) = 1 - c*(1-0.5^alpha)
    :param h_2:  - I(0.5) - I(0) = c*0.5^alpha

    The flux value outside of transit is 1. The light curve is calculated using
    the qpower2 algorithm, which is fast but only accurate for k < ~0.3.

    If the input parameters are invalid or k>0.5 the model is returned as an
    array of value 1 everywhere.

    """

    def __init__(self, independent_vars=['t'], prefix='', nan_policy='raise',
                 **kwargs):
        kwargs.update({'prefix': prefix, 'nan_policy': nan_policy,
                       'independent_vars': independent_vars})

        def _transit_func(t, T_0, P, D, W, S, f_c, f_s, h_1, h_2):
            if D <= 0:
                return np.ones_like(t)
            k = np.sqrt(D)
            if k > 0.5:
                return np.ones_like(t)
            if (1-S) <= 0:
                return np.ones_like(t)
            bsq = ((1-k)**2 - S*(1+k)**2) / (1-S) 
            if bsq < 0:
                return np.ones_like(t)
            b = np.sqrt(bsq)
            q = (1+k)**2-bsq
            if q <= 0:
                return np.ones_like(t)
            r_star = np.pi * W / np.sqrt(q)
            q = 1 - (b*r_star)**2
            if q <= 0:
                return np.ones_like(t)
            sini = np.sqrt(q)
            if (h_1 <= 0) or (h_2 <= 0) or (h_2 > h_1) or (h_1 >= 1):
                return np.ones_like(t)
            c2 = 1 - h_1 + h_2
            a2 = np.log2(c2/h_2)
            ecc = f_c**2 + f_s**2
            om = np.arctan2(f_s, f_c)*180/np.pi
            z,m = t2z(t, T_0, P, sini, r_star, ecc, om, returnMask = True)
            # Set z values where planet is behind star to a large nominal value
            z[m]  = 9999
            return qpower2(z, k, c2, a2)

        super(TransitModel, self).__init__(_transit_func, **kwargs)
        self._set_paramhints_prefix()

    def _set_paramhints_prefix(self):
        self.set_param_hint('P', min=1e-15)
        self.set_param_hint('D', min=0, max=1)
        self.set_param_hint('W', min=0, max=0.3)
        self.set_param_hint('S', min=0, max=1)
        self.set_param_hint('f_c', value=0, min=-1, max=1)
        self.set_param_hint('f_s', value=0, min=-1, max=1)
        self.set_param_hint('h_1', min=0, max=1)
        self.set_param_hint('h_2', min=0, max=1)
        expr = "sqrt({p:s}D)".format(p=self.prefix)
        self.set_param_hint('k'.format(p=self.prefix), 
                expr=expr, min=0, max=1)
        expr = ("sqrt(((1-{p:s}k)**2-{p:s}S*(1+{p:s}k)**2)/(1-{p:s}S))".
                format(p=self.prefix))
        self.set_param_hint('b', min=0, max=1.3, expr=expr)
        expr ="sqrt((1+{p:s}k)**2-{p:s}b**2)/{p:s}W/pi".format(p=self.prefix)
        self.set_param_hint('aR',min=1, expr=expr)
        expr = "0.013418*{p:s}aR**3/{p:s}P**2".format(p=self.prefix)
        self.set_param_hint('rho', min=0, expr = expr)

class EclipseModel(Model):
    r"""Light curve model for the eclipse by a spherical star of a spherical
    body (planet) with no limb darkening.

     The geometry of the system is defined using the parameters D, W and S, as
    defined below in terms of the star and planet radii, R_s and R_p,
    respectively, the semi-major axis, a, and the orbital inclination, i.
    These are the same parameters used in TransitModel. The flux level outside
    of eclipse is 1 and inside eclipse is (1-L). The apparent time of
    mid-eclipse includes the correction a_c for the light travel time across
    the orbit, i.e., for a circular orbit the time of mid-eclipse is (T_0 +
    0.5*P) + a_c. N.B. a_c has the same units as P.

     The following parameters are used for convenience - k = R_p/R_s, aR =
    a/R_s, b=aR.cos(i), J = L/D. These parameters are all available as
    constraints within the model. Also available is the mean stellar density
    in solar units, rho=0.013418*aR**3/(P/days)**2. N.B. this value of rho
    assumes that M_planet << M_star.

     The eccentricity and longitude of periastron for the planet's orbit are
    ecc and omega, respectively. 

    :param t:   - independent variable (time)
    :param T_0: - time of mid-transit
    :param P:   - orbital period
    :param D:   - (R_p/R_s)^2 = k^2
    :param W:   - (R_s/a)*sqrt((1+k)^2 - b^2)/pi
    :param S:   - ((1-k)^2-b^2)/((1+k)^2 - b^2)
    :param L:   - Depth of eclipse
    :param f_c: - sqrt(ecc).cos(omega)
    :param f_s: - sqrt(ecc).sin(omega)
    :param a_c: - correction for light travel time across the orbit

    """

    def __init__(self, independent_vars=['t'], prefix='', nan_policy='raise',
                 **kwargs):
        kwargs.update({'prefix': prefix, 'nan_policy': nan_policy,
                       'independent_vars': independent_vars})

        def _eclipse_func(t, T_0, P, D, W, S, L, f_c, f_s, a_c):
            if D <= 0:
                return np.ones_like(t)
            k = np.sqrt(D)
            if k > 0.5:
                return np.ones_like(t)
            if (1-S) <= 0:
                return np.ones_like(t)
            bsq = ((1-k)**2 - S*(1+k)**2) / (1-S) 
            if bsq < 0:
                return np.ones_like(t)
            b = np.sqrt(bsq)
            q = (1+k)**2-bsq
            if q <= 0:
                return np.ones_like(t)
            r_star = np.pi * W / np.sqrt(q)
            q = 1 - (b*r_star)**2
            if q <= 0:
                return np.ones_like(t)
            sini = np.sqrt(q)
            ecc = f_c**2 + f_s**2
            om = np.arctan2(f_s, f_c)*180/np.pi
            z,m = t2z(t-a_c, T_0, P, sini, r_star, ecc, om, returnMask=True)
            z[~m]  = 9999
            return 1 + L*(ueclipse(z, k)-1)

        super(EclipseModel, self).__init__(_eclipse_func, **kwargs)
        self._set_paramhints_prefix()

    def _set_paramhints_prefix(self):
        self.set_param_hint('P', min=1e-15)
        self.set_param_hint('D', min=0, max=1)
        self.set_param_hint('W', min=0, max=0.3)
        self.set_param_hint('S', min=0, max=1)
        self.set_param_hint('L', min=0, max=1)
        self.set_param_hint('f_c', value=0, min=-1, max=1, vary=False)
        self.set_param_hint('f_s', value=0, min=-1, max=1, vary=False)
        self.set_param_hint('a_c', value=0, min=0, vary=False)
        expr = "sqrt({prefix:s}D)".format(prefix=self.prefix)
        self.set_param_hint('k', expr=expr, min=0, max=1)
        expr = "D/L".format(prefix=self.prefix)
        self.set_param_hint('J', expr=expr, min=0)
        self.set_param_hint('aR', min=1, 
                expr="2/(pi*{p:s}W*sqrt((1-{p:s}S)/{p:s}k))"
                .format(p=self.prefix) )
        self.set_param_hint('rho', min=0, expr = 
                "0.013418*{p:s}aR**3/{p:s}P**2".format(p=self.prefix) )
        self.set_param_hint('b', max=1.3,
                expr = "sqrt(((1-{p:s}k)**2-{p:s}S*(1+{p:s}k)**2)/(1-{p:s}S))"
                .format(p=self.prefix) )

class FactorModel(Model):
    """Flux scaling and trend factor model

    f = c*(1 + dfdx*dx(t) + dfdy*dy(t) + d2fdx2*dx(t)**2 + d2f2y2*dy(t)**2 +
        d2fdxdy*x(t)*dy(t) + dfdsinphi*sin(phi(t)) + dfdcosphi*cos(phi(t)) +
        dfdsin2phi*sin(2.phi(t)) + dfdcos2phi*cos(2.phi(t)) + 
        dfdt*dt + d2fdt2*dt**2)

    The detrending coefficients dfdx, etc. are 0 and fixed by default. If
    any of the coefficients dfdx, d2fdxdy or d2f2x2 is not 0, a function to
    calculate the x-position offset as a function of time, dx(t), must be
    passed as a keyword argument, and similarly for the y-position offset,
    dy(t). For detrending against the spacecraft roll angle, phi(t), the
    functions to be provided as keywords arguments are sinphi(t) and
    cosphi(t). The time trend decribed by dfdt and d2fdt2 is calculated using
    the variable dt = t - median(t).

    """

    def __init__(self, independent_vars=['t'], prefix='', nan_policy='raise',
                dx=None, dy=None, sinphi=None, cosphi=None, **kwargs):
        kwargs.update({'prefix': prefix, 'nan_policy': nan_policy,
                       'independent_vars': independent_vars})

        def factor(t, d2fdt2=0, dfdt=0, dfdcosphi=0, dfdsinphi=0,
                dfdcos2phi=0, dfdsin2phi=0, d2fdy2=0, d2fdxdy=0, d2fdx2=0,
                dfdy=0, dfdx=0, c=1.0):

            dt = t - np.median(t)
            trend = 1 + dfdt*dt + d2fdt2*dt**2
            if dfdx != 0 or d2fdx2 != 0:
                trend += dfdx*self.dx(t) + d2fdx2*self.dx(t)**2
            if dfdy != 0 or d2fdy2 != 0:
                trend += dfdy*self.dy(t) + d2fdy2*self.dy(t)**2
            if d2fdxdy != 0 :
                trend += d2fdxdy*self.dx(t)*self.dy(t)
            if dfdsinphi != 0 :
                trend += dfdsinphi*self.sinphi(t)
            if dfdcosphi != 0 :
                trend += dfdcosphi*self.cosphi(t)
            if dfdsin2phi != 0 :
                sin2phi = 2*self.sinphi(t)*self.cosphi(t)
                trend += dfdsin2phi*sin2phi
            if dfdcos2phi != 0 :
                cos2phi = self.sinphi(t)**2 - self.cosphi(t)**2
                trend += dfdcos2phi*cos2phi
            return c*trend

        super(FactorModel, self).__init__(factor, **kwargs)

        self.dx = dx
        self.dy = dy
        self.sinphi = sinphi
        self.cosphi = cosphi
        self.set_param_hint('c', min=0)
        for p in ['dfdx', 'dfdy', 'd2fdx2', 'd2fdxdy',  'd2fdy2', 
                  'dfdsinphi', 'dfdcosphi', 'dfdt', 'd2fdt2']:
            self.set_param_hint(p, value=0, vary=False)

    def guess(self, data, **kwargs):
        """Estimate initial model parameter values from data."""
        pars = self.make_params()

        pars['%sc' % self.prefix].set(value=data.median())
        for p in ['dfdx', 'dfdy', 'd2fdx2', 'd2fdy2', 
                'dfdsinphi', 'dfdcosphi', 'dfdt', 'd2fdt2']:
            pars['{}{}'.format(self.prefix, p)].set(value = 0.0, vary=False)
        return update_param_vals(pars, self.prefix, **kwargs)

    __init__.__doc__ = COMMON_INIT_DOC
    guess.__doc__ = COMMON_GUESS_DOC

class ThermalPhaseModel(Model):
    """Thermal phase model for a tidally-locked planet
         a_th*(1-cos(phi))/2 + b_th*(1+sin(phi))/2 + c_th,
    where phi = 2*pi*(t-T_0)/P

    :param t:    - independent variable (time)
    :param T_0:  - time of inferior conjunction (mid-transit)
    :param P:    - orbital period
    :param a_th: - coefficient of cosine-like term
    :param b_th: - coefficient of sine-like term
    :param c_th: - constant term (minimum flux)

    The following parameters are defined for convenience.

    A = sqrt(a_th**2 + b_th**2), peak-to-trough amplitude of the phase curve
    F = c_th + (a_th + b_th + A)/2, flux at the maximum of the phase curve
    ph_max = arctan2(b_th,-a_th)/(2*pi) = phase at maximum flux

    """

    def __init__(self, independent_vars=['t'], prefix='', nan_policy='raise',
                 **kwargs):
        kwargs.update({'prefix': prefix, 'nan_policy': nan_policy,
                       'independent_vars': independent_vars})

        def _thermal_phase(t, T_0, P, a_th, b_th, c_th):
            phi = 2*np.pi*(t-T_0)/P
            return a_th*(1-np.cos(phi))/2 + b_th*(1+np.sin(phi))/2 + c_th

        super(ThermalPhaseModel, self).__init__(_thermal_phase, **kwargs)
        self._set_paramhints_prefix()

    def _set_paramhints_prefix(self):
        self.set_param_hint('P', min=1e-15)
        self.set_param_hint('a_th', value=0)
        self.set_param_hint('b_th', value=0)
        self.set_param_hint('c_th', value=0, min=0)
        expr = "hypot({p:s}a_th,{p:s}b_th)".format(p=self.prefix)
        self.set_param_hint('A', expr=expr)
        expr = "{p:s}c_th+({p:s}a_th+{p:s}b_th+{p:s}A)/2".format(p=self.prefix)
        self.set_param_hint('Fmax', expr=expr, min=0)
        expr = "{p:s}Fmax - {p:s}A".format(p=self.prefix)
        self.set_param_hint('Fmin', expr=expr, min=0)
        expr = "arctan2({p:s}b_th,-{p:s}a_th)/(2*pi)".format(p=self.prefix)
        self.set_param_hint('ph_max', expr=expr)

    __init__.__doc__ = COMMON_INIT_DOC


class ReflectionModel(Model):
    """Reflected stellar light from a planet with a Lambertian phase function.

     The fraction of the stellar flux reflected from the planet of radius R_p 
    at a distance r from the star and viewed at phase angle beta is
      A_g*(R_p/r)**2 * [sin(beta) + (pi-beta)*cos(beta) ]/pi
 
     The eccentricity and longitude of periastron for the planet's orbit are
    ecc and omega, respectively.

    :param t:    - independent variable (time)
    :param T_0:  - time of inferior conjunction (mid-transit)
    :param P:    - orbital period
    :param A_g:  - geometric albedo
    :param r_p:  - R_p/a, where a is the semi-major axis.
    :param f_c:  - sqrt(ecc).cos(omega)
    :param f_s:  - sqrt(ecc).sin(omega)
    :param sini: - sin(inclination)

    """

    def __init__(self, independent_vars=['t'], prefix='', nan_policy='raise',
                 **kwargs):
        kwargs.update({'prefix': prefix, 'nan_policy': nan_policy,
                       'independent_vars': independent_vars})

        def _reflection(t, T_0, P, A_g, r_p, f_c, f_s, sini):
            ecc = f_c**2 + f_s**2
            om = np.arctan2(f_s, f_c)*180/np.pi
            x,y,z = xyz_planet(t, T_0, P, sini, ecc, om)
            r = np.sqrt(x**2+y**2+z**2)
            beta = np.arccos(-z/r)
            Phi_L = (np.sin(beta) + (np.pi-beta)*np.cos(beta) )/np.pi
            return A_g*(r_p/r)**2*Phi_L

        super(ReflectionModel, self).__init__(_reflection, **kwargs)
        self._set_paramhints_prefix()

    def _set_paramhints_prefix(self):
        self.set_param_hint('P', min=1e-15)
        self.set_param_hint('A_g', value=0.5, min=0, max=1)
        self.set_param_hint('r_p', min=0, max=1)
        self.set_param_hint('f_c', value=0, vary=False, min=-1, max=1)
        self.set_param_hint('f_s', value=0, vary=False, min=-1, max=1)
        self.set_param_hint('sini', value=1, vary=False, min=0, max=1)

    __init__.__doc__ = COMMON_INIT_DOC

class RVModel(Model):
    """Radial velocity in a Keplerian orbit

    :param t:    - independent variable (time)
    :param T_0:  - time of inferior conjunction for the companion (mid-transit)
    :param P:    - orbital period
    :param V_0:  - radial velocity of the centre-of-mass
    :param K:    - semi-amplitude of spectroscopic orbit
    :param f_c:  - sqrt(ecc).cos(omega)
    :param f_s:  - sqrt(ecc).sin(omega)
    :param sini: - sine of the orbital inclination

    """

    def __init__(self, independent_vars=['t'], prefix='', nan_policy='raise',
                 **kwargs):
        kwargs.update({'prefix': prefix, 'nan_policy': nan_policy,
                       'independent_vars': independent_vars})

        def _rv(t, T_0, P, V_0, K, f_c, f_s, sini):
            ecc = f_c**2 + f_s**2
            om = np.arctan2(f_s, f_c)*180/np.pi
            return V_0 + vrad(t, T_0, P, K, ecc, om, sini, primary=True)

        super(RVModel, self).__init__(_rv, **kwargs)
        self._set_paramhints_prefix()

    def _set_paramhints_prefix(self):
        self.set_param_hint('P', min=1e-15)
        self.set_param_hint('K', min=1e-15)
        self.set_param_hint('f_c', value=0, vary=False, min=-1, max=1)
        self.set_param_hint('f_s', value=0, vary=False, min=-1, max=1)
        self.set_param_hint('sini', value=1, vary=False, min=0, max=1)
        expr = "{p:s}f_c**2 + {p:s}f_s**2".format(p=self.prefix)
        self.set_param_hint('{p:s}e'.format(p=self.prefix), expr=expr, 
                min=0, max=1)
        expr = "180*atan2({p:s}f_s, {p:s}f_c)/pi".format(p=self.prefix)
        self.set_param_hint('{p:s}omega'.format(p=self.prefix), expr=expr) 

    __init__.__doc__ = COMMON_INIT_DOC


class RVCompanion(Model):
    """Radial velocity in a Keplerian orbit for the companion


    :param t:    - independent variable (time)
    :param T_0:  - time of inferior conjunction for the companion (mid-transit)
    :param P:    - orbital period
    :param V_0:  - radial velocity of the centre-of-mass
    :param K:    - semi-amplitude of spectroscopic orbit
    :param f_c:  - sqrt(ecc).cos(omega)
    :param f_s:  - sqrt(ecc).sin(omega)
    :param sini: - sine of the orbital inclination

    """

    def __init__(self, independent_vars=['t'], prefix='', nan_policy='raise',
                 **kwargs):
        kwargs.update({'prefix': prefix, 'nan_policy': nan_policy,
                       'independent_vars': independent_vars})

        def _rv(t, T_0, P, V_0, K, f_c, f_s, sini):
            ecc = f_c**2 + f_s**2
            om = np.arctan2(f_s, f_c)*180/np.pi
            return V_0 + vrad(t, T_0, P, K, ecc, om, sini, primary=False)

        super(RVCompanion, self).__init__(_rv, **kwargs)
        self._set_paramhints_prefix()

    def _set_paramhints_prefix(self):
        self.set_param_hint('P', min=1e-15)
        self.set_param_hint('K', min=1e-15)
        self.set_param_hint('f_c', value=0, vary=False, min=-1, max=1)
        self.set_param_hint('f_s', value=0, vary=False, min=-1, max=1)
        self.set_param_hint('sini', value=1, vary=False, min=0, max=1)
        expr = "{p:s}f_c**2 + {p:s}f_s**2".format(p=self.prefix)
        self.set_param_hint('{p:s}e'.format(p=self.prefix), expr=expr, 
                min=0, max=1)
        expr = "180*atan2({p:s}f_s, {p:s}f_c)/pi".format(p=self.prefix)
        self.set_param_hint('{p:s}omega'.format(p=self.prefix), expr=expr)

    __init__.__doc__ = COMMON_INIT_DOC

class Priors(OrderedDict):
    """An ordered dictionary of all the Prior objects required to evaluate the
    log-value of the prior for Bayesian model fitting. 

    All values of a Priors() instance must be Prior objects.

    A Priors() instance includes an asteval interpreter used for
    evaluation of constrained Parameters.

    ToDo: copying, pickling and serialization

    """

    def __init__(self, usersyms=None):
        """
        Arguments
        ---------
        usersyms : dictionary of symbols to add to the
            :class:`asteval.Interpreter`.

        """

        super(Parameters, self).__init__(self)
        self._asteval = Interpreter(usersyms=usersyms)



class Prior(object):
    """A Prior is an object that can be used in the calculation of the
    log-likelihood function for Bayeisan model fitting methods.

    A Prior has a `name` attribute that corresponds to the name of one of the
    parameters in the model, i.e., there must be a corresponding Parameter
    object in the model with the same name. The log-value of the prior for a
    given parameter value is evaluated using the mathemetical expression
    provided in `expr`, e.g., for a Gaussian with mean mu and standard
    deviation sigma, the contribution to the total log-likelihood for a
    parameter with value x is -0.5*(mu-x)**2/sigma**2. The constants mu and
    sigma are hyper-parameters, i.e., parameters of the prior model, not of
    the data model.

    """

    def __init__(self, name=None, expr=None, hyper=None):
        """
        Parameters
        ----------

        name : str
            Name of the Parameter to which the Prior is applied.
        expr : str
            Mathematical expression used to evaluate the prior log-value
        hyper : dict
            A dictionary of hyper-parameters.

        """

        self.name = name
        self.expr = expr
        self.hyper = hyper

    def __repr__(self):
        """Return printable representation of a Parameter object."""
        return "<Prior {}> : {}".format(self.name, self.expr)


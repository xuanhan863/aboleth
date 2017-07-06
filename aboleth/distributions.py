"""Model parameter distributions."""
import numpy as np
import tensorflow as tf
from multipledispatch import dispatch

from aboleth.util import pos
from aboleth.random import seedgen


#
# Generic prior and posterior classes
#

class Normal:
    """
    Normal (IID) prior/posterior.

    Parameters
    ----------
    mu : Tensor
        mean, shape [d_i, d_o]
    var : Tensor
        variance, shape [d_i, d_o]
    """

    def __init__(self, mu=0., var=1.):
        """Construct a Normal distribution object."""
        self.mu = mu
        self.var = var
        self.sigma = tf.sqrt(var)
        self.d = tf.shape(mu)

    def sample(self):
        """Draw a random sample from this object."""
        # Reparameterisation trick
        e = tf.random_normal(self.d, seed=next(seedgen))
        x = self.mu + e * self.sigma
        return x


class Gaussian:
    """
    Gaussian prior/posterior.

    Parameters
    ----------
    mu : Tensor
        mean, shape [d_i, d_o]
    L : Tensor
        Cholesky of the covariance matrix, shape [d_o, d_i, d_i]
    """

    def __init__(self, mu, L):
        """Construct a Normal distribution object."""
        self.mu = mu
        self.L = L  # O x I x I
        self.d = tf.shape(mu)

    def sample(self):
        """Construct a Normal distribution object."""
        # Reparameterisation trick
        mu = self.transform_w(self.mu)
        e = tf.random_normal(tf.shape(mu), seed=next(seedgen))
        x = self.itransform_w(mu + tf.matmul(self.L, e))
        return x

    @staticmethod
    def transform_w(w):
        """Transform a weight matrix, [d_i, d_o] -> [d_o, d_i, 1]."""
        wt = tf.expand_dims(tf.transpose(w), 2)  # O x I x 1
        return wt

    @staticmethod
    def itransform_w(wt):
        """Un-transform a weight matrix, [d_o, d_i, 1] -> [d_i, d_o]."""
        w = tf.transpose(wt[:, :, 0])
        return w


#
# Streamlined interfaces for initialising the priors and posteriors
#

def norm_prior(dim, var):
    """Initialise a prior (diagonal) Normal distribution.

    Parameters
    ----------
    dim : tuple or list
        the dimension of this distribution.
    var : float
        the prior variance of this distribution.

    Returns
    -------
    Q : Normal
        the initialised prior Normal object.
    """
    mu = tf.zeros(dim)
    var = pos(tf.Variable(var, name="W_mu_p"))
    P = Normal(mu, var)
    return P


def norm_posterior(dim, var0):
    """Initialise a posterior (diagonal) Normal distribution.

    Parameters
    ----------
    dim : tuple or list
        the dimension of this distribution.
    var0 : float
        the initial (unoptimized) variance of this distribution.

    Returns
    -------
    Q : Normal
        the initialised posterior Normal object.
    """
    mu_0 = tf.random_normal(dim, stddev=np.sqrt(var0), seed=next(seedgen))
    mu = tf.Variable(mu_0, name="W_mu_q")

    var_0 = tf.random_gamma(alpha=var0, shape=dim, seed=next(seedgen))
    var = pos(tf.Variable(var_0, name="W_var_q"))

    Q = Normal(mu, var)
    return Q


def gaus_posterior(dim, var0):
    """Initialise a posterior Gaussian distribution with a diagonal covariance.

    Even though this is initialised with a diagonal covariance, a full
    covariance will be learned.

    Parameters
    ----------
    dim : tuple or list
        the dimension of this distribution.
    var0 : float
        the initial (unoptimized) diagonal variance of this distribution.

    Returns
    -------
    Q : Gaussian
        the initialised posterior Gaussian object.
    """
    I, O = dim
    sig0 = np.sqrt(var0)

    # Optimize only values in lower triangular
    u, v = np.tril_indices(I)
    indices = (u * I + v)[:, np.newaxis]
    l0 = np.tile(np.eye(I), [O, 1, 1])[:, u, v].T
    l0 = l0 * tf.random_gamma(alpha=sig0, shape=l0.shape, seed=next(seedgen))
    l = tf.Variable(l0, name="W_cov_q")
    Lt = tf.transpose(tf.scatter_nd(indices, l, shape=(I * I, O)))
    L = tf.reshape(Lt, (O, I, I))

    mu_0 = tf.random_normal((I, O), stddev=sig0, seed=next(seedgen))
    mu = tf.Variable(mu_0, name="W_mu_q")
    Q = Gaussian(mu, L)
    return Q


#
# KL divergence calculations
#


@dispatch(Normal, Normal)
def kl_qp(q, p):
    """Normal-Normal Kullback Leibler divergence calculation.

    Parameters
    ----------
    q : Normal
        the approximating 'q' distribution.
    p : Normal
        the prior 'p' distribution.

    Returns
    -------
    KL : Tensor
        the result of KL[q||p].
    """
    KL = 0.5 * (tf.log(p.var) - tf.log(q.var) + q.var / p.var - 1. +
                (q.mu - p.mu)**2 / p.var)
    KL = tf.reduce_sum(KL)
    return KL


@dispatch(Gaussian, Normal)  # noqa
def kl_qp(q, p):
    """Gaussian-Normal Kullback Leibler divergence calculation.

    Parameters
    ----------
    q : Gaussian
        the approximating 'q' distribution.
    p : Normal
        the prior 'p' distribution.

    Returns
    -------
    KL : Tensor
        the result of KL[q||p].
    """
    D, n = tf.to_float(q.d[0]), tf.to_float(q.d[1])
    tr = tf.reduce_sum(q.L * q.L) / p.var
    dist = tf.reduce_sum((p.mu - q.mu)**2) / p.var
    logdet = n * D * tf.log(p.var) - _chollogdet(q.L)
    KL = 0.5 * (tr + dist + logdet - n * D)
    return KL


@dispatch(Gaussian, Gaussian)  # noqa
def kl_qp(q, p):
    """Gaussian-Gaussian Kullback Leibler divergence calculation.

    Parameters
    ----------
    q : Gaussian
        the approximating 'q' distribution.
    p : Gaussian
        the prior 'p' distribution.

    Returns
    -------
    KL : Tensor
        the result of KL[q||p].
    """
    D, n = tf.to_float(q.d[0]), tf.to_float(q.d[1])
    qCipC = tf.cholesky_solve(p.L, tf.matmul(q.L, q.L, transpose_b=True))
    tr = tf.reduce_sum(tf.trace(qCipC))
    md = q.transform_w(p.mu - q.mu)
    dist = tf.reduce_sum(md * tf.cholesky_solve(p.L, md))
    logdet = _chollogdet(p.L) - _chollogdet(q.L)
    KL = 0.5 * (tr + dist + logdet - n * D)
    return KL


#
# Private module stuff
#

def _chollogdet(L):
    """Log det of a cholesky, where L is [..., D, D]."""
    l = tf.maximum(tf.matrix_diag_part(L), 1e-15)  # Make sure we don't go to 0
    logdet = 2. * tf.reduce_sum(tf.log(l))
    return logdet

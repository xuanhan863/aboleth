"""Network layers and utilities."""
import numpy as np
import tensorflow as tf

from aboleth.random import seedgen
from aboleth.distributions import (norm_prior, norm_posterior, gaus_posterior,
                                   kl_qp)


#
# Input layers
#

class InputLayer:
    """Create an input layer.

    This layer defines input kwargs so that a user may easily provide the right
    inputs to a complex set of layers. It takes a 2D tensor of shape (k, d).
    If n_samples is specified, the input is tiled along a new first axis
    creating a (n,k,d) tensor for propogating samples through a variational
    deep net.

    Parameters
    ----------
    name : string
        The name of the input. Used as the agument for input into the net.

    n_samples : int > 0
        The number of samples.

    """

    def __init__(self, name, n_samples=None):
        """Construct an instance of InputLayer."""
        self.name = name
        self.n_samples = n_samples

    def __call__(self, **kwargs):
        """Build the tiling input layer."""
        X = kwargs[self.name]
        if self.n_samples is not None:
            # (n_samples, N, D)
            Xs = tf.tile(tf.expand_dims(X, 0), [self.n_samples, 1, 1])
        else:
            Xs = tf.convert_to_tensor(X)
        return Xs, 0.0


#
# Generic Layers
#

class Layer:
    """Layer base class.

    This is an identity layer, and is primarily meant to be subclassed to
    construct more intersting layers.
    """

    def __call__(self, X):
        """Build the graph of this layer.

        See: build

        """
        Net, KL = self.build(X)
        return Net, KL

    def build(self, X):
        """Build the graph of this layer.

        Parameters
        ----------
        X : Tensor
            the input to this layer

        Returns
        -------
        Net : Tensor
            the output of this layer
        KL : {float, Tensor}
            the regularizer/Kullback Liebler 'cost' of the parameters in this
            layer.

        """
        return X, 0.0


class SampleLayer(Layer):
    """Sample Layer base class.

    This is the base class for layers that build upon stochastic (variational)
    nets. These expect *rank 3* input Tensors, where the first dimension
    indexes the random samples of the stochastic net.
    """

    def __call__(self, X):
        """Build the graph of this layer.

        See: build

        """
        rank = len(X.shape)
        assert rank == 3
        Net, KL = self.build(X)
        return Net, KL

    @staticmethod
    def get_X_dims(X):
        """Get the dimensions of the rank 3 input tensor."""
        n_samples, input_dim = int(X.shape[0]), int(X.shape[2])
        return n_samples, input_dim


#
# Activation Layers
#

class Activation(Layer):
    """Activation function layer.

    Parameters
    ----------
    h : callable
        the *element-wise* activation function.

    """

    def __init__(self, h=lambda X: X):
        """Create an instance of an Activation layer."""
        self.h = h

    def build(self, X):
        """Build the graph of this layer."""
        Net = self.h(X)
        KL = 0.
        return Net, KL


class DropOut(Layer):
    """Dropout layer, Bernoulli probability of not setting an input to zero.

    This is just a thin wrapper around `tf.dropout
    <https://www.tensorflow.org/api_docs/python/tf/nn/dropout>`_

    Parameters
    ----------
    keep_prob : float, Tensor
        the probability of keeping an input. See `tf.dropout
        <https://www.tensorflow.org/api_docs/python/tf/nn/dropout>`_.

    """

    def __init__(self, keep_prob):
        """Create an instance of a Dropout layer."""
        self.keep_prob = keep_prob

    def build(self, X):
        """Build the graph of this layer."""
        noise_shape = None  # equivalent to different samples from posterior
        Net = tf.nn.dropout(X, self.keep_prob, noise_shape, seed=next(seedgen))
        KL = 0.
        return Net, KL


#
# Kernel Approximation Layers
#

class RandomRBF(SampleLayer):
    """Random radial basis function (RBF) Fourier feature layer.

    NOTE: This should be followed by a dense layer to properly implement a
        kernel approximation.

    Parameters
    ----------
    n_features : int
        the number of unique random features, the actual output dimension of
        this layer will be ``2 * n_features``.
    lenscale : float, ndarray, Tensor
        the lenght scales of the radial basis kernel, this can be a scalar for
        an isotropic kernel, or a vector for an automatic relevance detection
        (ARD) kernel.

    """

    def __init__(self, n_features, lenscale=1.0):
        """Create an instance of an RBF kernel layer."""
        self.n_features = n_features
        self.lenscale = lenscale

    def build(self, X):
        """Build the graph of this layer."""
        n_samples, input_dim = self.get_X_dims(X)

        # Random weights, copy faster than map here
        P = self.weights(input_dim)
        Ps = tf.tile(tf.expand_dims(P, 0), [n_samples, 1, 1])

        # Random features
        XP = tf.matmul(X, Ps)
        real = tf.cos(XP)
        imag = tf.sin(XP)
        Net = tf.concat([real, imag], axis=-1) / np.sqrt(self.n_features)
        KL = 0.

        return Net, KL

    def weights(self, input_dim):
        """Generate the random fourier weights for this kernel.

        Parameters
        ----------
        input_dim : int
            the input dimension to this layer.
        n_features : int
            the number of unique random features, the actual output dimension
            of this layer will be ``2 * n_features``.

        Returns
        -------
        P : ndarray
            the random weights of the fourier features of shape
            ``(input_dim, n_features)``.

        """
        rand = np.random.RandomState(next(seedgen))
        P = rand.randn(input_dim, self.n_features).astype(np.float32)
        return P / self.lenscale


class RandomMatern(RandomRBF):
    """Random Matern Fourier feature layer.

    NOTE: This should be followed by a dense layer to properly implement a
        kernel approximation.

    Parameters
    ----------
    n_features : int
        the number of unique random features, the actual output dimension of
        this layer will be ``2 * n_features``.
    lenscale : float, ndarray, Tensor
        the lenght scales of the radial basis kernel, this can be a scalar for
        an isotropic kernel, or a vector for an automatic relevance detection
        (ARD) kernel.
    p : int
        this is the Matern kernel 'number', i.e. a v = 3/2 kernel would be p =
        1, the rule is :math:`v = p + .5`. This has to be an integer greater
        than or equal to 0.

    """

    def __init__(self, n_features, lenscale=1.0, p=1):
        """Create an instance of an Matern kernel layer."""
        super().__init__(n_features, lenscale)
        assert isinstance(p, int) and p >= 0
        self.p = p

    def weights(self, input_dim):
        """Generate the random fourier weights for this kernel.

        Parameters
        ----------
        input_dim : int
            the input dimension to this layer.
        n_features : int
            the number of unique random features, the actual output dimension
            of this layer will be ``2 * n_features``.

        Returns
        -------
        P : ndarray
            the random weights of the fourier features of shape
            ``(input_dim, n_features)``.

        """
        # p is the matern number (v = p + .5) and the two is a transformation
        # of variables between Rasmussen 2006 p84 and the CF of a Multivariate
        # Student t (see wikipedia). Also see "A Note on the Characteristic
        # Function of Multivariate t Distribution":
        #   http://ocean.kisti.re.kr/downfile/volume/kss/GCGHC8/2014/v21n1/
        #   GCGHC8_2014_v21n1_81.pdf
        # To sample from a m.v. t we use the formula
        # from wikipedia, x = y * np.sqrt(df / u) where y ~ norm(0, I),
        # u ~ chi2(df), then x ~ mvt(0, I, df)
        df = 2 * (self.p + 0.5)
        rand = np.random.RandomState(next(seedgen))
        y = rand.randn(input_dim, self.n_features)
        u = rand.chisquare(df, size=(self.n_features,))
        P = y * np.sqrt(df / u)
        P = P.astype(np.float32)
        return P / self.lenscale


class RandomArcCosine(SampleLayer):
    """Random arc-cosine kernel layer.

    NOTE: This should be followed by a dense layer to properly implement a
        kernel approximation.

    Parameters
    ----------
    n_features : int
        the number of unique random features, the actual output dimension of
        this layer will be ``2 * n_features``.
    lenscale : float, ndarray, Tensor
        the lenght scales of the ar-cosine kernel, this can be a scalar for
        an isotropic kernel, or a vector for an automatic relevance detection
        (ARD) kernel.
    p : int
        The order of the arc-cosine kernel, this must be an integer greater
        than, or eual to zero. 0 will lead to sigmoid-like kernels, 1 will lead
        to relu-like kernels, 2 quadratic-relu kernels etc.

    See Also
    --------
    [1] Cho, Youngmin, and Lawrence K. Saul. "Analysis and extension of
        arc-cosine kernels for large margin classification." arXiv preprint
        arXiv:1112.3712 (2011).
    [2] Cutajar, Kurt, Edwin V. Bonilla, Pietro Michiardi, and Maurizio
        Filippone. "Accelerating Deep Gaussian Processes Inference with
        Arc-Cosine Kernels." Bayesian Deep Learning Workshop, Advances in
        Neural Information Processing Systems, NIPS 2016, Barcelona

    """

    def __init__(self, n_features, lenscale=1.0, p=1):
        """Create an instance of an arc cosine kernel layer."""
        assert isinstance(p, int) and p >= 0
        if p == 0:
            self.pfunc = tf.sign
        elif p == 1:
            self.pfunc = lambda x: x
        else:
            self.pfunc = lambda x: tf.pow(x, p)

        self.n_features = n_features
        self.lenscale = lenscale

    def build(self, X):
        """Build the graph of this layer."""
        n_samples, input_dim = self.get_X_dims(X)

        # Random weights
        rand = np.random.RandomState(next(seedgen))
        P = rand.randn(input_dim, self.n_features).astype(np.float32) \
            / self.lenscale
        Ps = tf.tile(tf.expand_dims(P, 0), [n_samples, 1, 1])

        # Random features
        XP = tf.matmul(X, Ps)
        Net = np.sqrt(2. / self.n_features) * tf.nn.relu(self.pfunc(XP))
        KL = 0.

        return Net, KL


#
# Weight layers
#

class DenseVariational(SampleLayer):
    """Dense (fully connected) linear layer, with variational inference.

    Parameters
    ----------
    output_dim : int
        the dimension of the output of this layer
    reg : float
        the initial value of the weight prior, w ~ N(0, reg * I), this is
        optimized (a la maximum likelihood type II).
    full : bool
        If true, use a full covariance Gaussian posterior for *each* of the
        output weight columns, otherwise use an independent (diagonal) Normal
        posterior.
    use_bias : bool
        If true, also learn a bias weight, e.g. a constant offset weight.
    prior_W : {Normal, Gaussian}, optional
        This is the prior distribution object to use on the layer weights. It
        must have parameters compatible with ``(input_dim, output_dim)`` shaped
        weights. This ignores the ``reg`` parameter.
    prior_b : {Normal, Gaussian}, optional
        This is the prior distribution object to use on the layer intercept. It
        must have parameters compatible with ``(output_dim,)`` shaped weights.
        This ignores the ``reg`` and ``use_bias`` parameters.
    post_W : {Normal, Gaussian}, optional
        This is the posterior distribution object to use on the layer weights.
        It must have parameters compatible with ``(input_dim, output_dim)``
        shaped weights. This ignores the ``full`` parameter. See
        ``distributions.gaus_posterior``.
    post_b : {Normal, Gaussian}, optional
        This is the posterior distribution object to use on the layer
        intercept. It must have parameters compatible with ``(output_dim,)``
        shaped weights. This ignores the ``use_bias`` parameters.
        See ``distributions.norm_posterior``.

    """

    def __init__(self, output_dim, reg=1., full=False, use_bias=True,
                 prior_W=None, prior_b=None, post_W=None, post_b=None):
        """Create and instance of a variational dense layer."""
        self.output_dim = output_dim
        self.reg = reg
        self.full = full
        self.use_bias = use_bias
        self.pW = prior_W
        self.pb = prior_b
        self.qW = post_W
        self.qb = post_b

    def build(self, X):
        """Build the graph of this layer."""
        n_samples, input_dim = self.get_X_dims(X)

        # Layer weights
        self.pW = self._make_prior(self.pW, input_dim)
        self.qW = self._make_posterior(self.qW, input_dim)

        # Regularizers
        KL = kl_qp(self.qW, self.pW)

        # Linear layer
        Wsamples = self._sample_W(self.qW, n_samples)
        Net = tf.matmul(X, Wsamples)

        # Optional bias
        if self.use_bias is True or self.prior_b or self.post_b:
            # Layer intercepts
            self.pb = self._make_prior(self.pb)
            self.qb = self._make_posterior(self.qb)

            # Regularizers
            KL += kl_qp(self.qb, self.pb)

            # Linear layer
            bsamples = tf.expand_dims(self._sample_W(self.qb, n_samples), 1)
            Net += bsamples

        return Net, KL

    def _make_prior(self, prior_W, input_dim=None):
        """Check/make prior."""
        dim = (input_dim, self.output_dim) if input_dim else (self.output_dim,)
        if prior_W:
            assert _is_dim(prior_W.mu, dim), "Prior inconsistent dimension!"
        else:
            prior_W = norm_prior(dim=dim, var=self.reg)
        return prior_W

    def _make_posterior(self, post_W, input_dim=None):
        """Check/make posterior."""
        dim = (input_dim, self.output_dim) if input_dim else (self.output_dim,)
        if post_W:
            assert _is_dim(post_W.mu, dim), "Posterior inconsistent dimension!"
        else:
            # We don't want a full-covariance on an intercept, check input_dim
            fullcov = self.full and input_dim
            post_W = (gaus_posterior(dim=dim, var0=self.reg) if fullcov else
                      norm_posterior(dim=dim, var0=self.reg))
        return post_W

    @staticmethod
    def _sample_W(dist, n_samples):
        samples = tf.stack([dist() for _ in range(n_samples)])
        return samples


class EmbedVariational(DenseVariational):
    """Dense (fully connected) embedding layer, with variational inference.

    This layer works directly on shape (N, 1) inputs of category *indices*
    rather than one-hot representations, for efficiency.

    Parameters
    ----------
    output_dim : int
        the dimension of the output (embedding) of this layer
    n_categories : int
        the number of categories in the input variable
    reg : float
        the initial value of the weight prior, w ~ N(0, reg * I), this is
        optimized (a la maximum likelihood type II)
    full : bool
        If true, use a full covariance Gaussian posterior for *each* of the
        output weight columns, otherwise use an independent (diagonal) Normal
        posterior.
    prior_W : {Normal, Gaussian}, optional
        This is the prior distribution object to use on the layer weights. It
        must have parameters compatible with ``(input_dim, output_dim)`` shaped
        weights. This ignores the ``reg`` parameter.
    post_W : {Normal, Gaussian}, optional
        This is the posterior distribution object to use on the layer weights.
        It must have parameters compatible with ``(input_dim, output_dim)``
        shaped weights. This ignores the ``full`` parameter. See
        ``distributions.gaus_posterior``.

    """

    def __init__(self, output_dim, n_categories, reg=1., full=False,
                 prior_W=None, post_W=None):
        """Create and instance of a variational dense embedding layer."""
        assert n_categories >= 2, "Need 2 or more categories for embedding!"
        self.output_dim = output_dim
        self.n_categories = n_categories
        self.reg = reg
        self.full = full
        self.pW = prior_W
        self.qW = post_W

    def build(self, X):
        """Build the graph of this layer."""
        n_samples, input_dim = self.get_X_dims(X)
        assert input_dim == 1, "X must be a *column* of indices!"

        # Layer weights
        self.pW = self._make_prior(self.pW, self.n_categories)
        self.qW = self._make_posterior(self.qW, self.n_categories)

        # Embedding layer -- gather only works on the first dim hence transpose
        Wsamples = tf.transpose(self._sample_W(self.qW, n_samples), [1, 2, 0])
        embedding = tf.gather(Wsamples, X[0, :, 0])  # X ind is just replicated
        Net = tf.transpose(embedding, [2, 0, 1])  # reshape after index 1st dim

        # Regularizers
        KL = kl_qp(self.qW, self.pW)

        return Net, KL


class DenseMAP(SampleLayer):
    """Dense (fully connected) linear layer, with MAP inference.

    Parameters
    ----------
    output_dim : int
        the dimension of the output of this layer
    l1_reg : float
        the value of the l1 weight regularizer, reg * ||w||_1
    l2_reg : float
        the value of the l2 weight regularizer, reg * 0.5 * ||w||^2_2
    use_bias : bool
        If true, also learn a bias weight, e.g. a constant offset weight.

    """

    def __init__(self, output_dim, l1_reg=1., l2_reg=1., use_bias=True):
        """Create and instance of a dense layer with MAP regularizers."""
        self.output_dim = output_dim
        self.l1 = l1_reg
        self.l2 = l2_reg
        self.use_bias = use_bias

    def build(self, X):
        """Build the graph of this layer."""
        n_samples, input_dim = self.get_X_dims(X)
        Wdim = (input_dim, self.output_dim)

        W = tf.Variable(tf.random_normal(shape=Wdim, seed=next(seedgen)),
                        name="W_map")

        # We don't want to copy tf.Variable W so map over X
        Net = tf.map_fn(lambda x: tf.matmul(x, W), X)

        # Regularizers
        penalty = self.l2 * tf.nn.l2_loss(W) + self.l1 * _l1_loss(W)

        # Optional Bias
        if self.use_bias is True:
            b = tf.Variable(tf.zeros(self.output_dim), name="b_map")
            Net += b
            penalty += self.l2 * tf.nn.l2_loss(b) + self.l1 * _l1_loss(b)

        return Net, penalty


#
# Private module stuff
#

def _l1_loss(X):
    """Calculate the L1 loss, |X|."""
    l1 = tf.reduce_sum(tf.abs(X))
    return l1


def _is_dim(X, dims):
    """Check if ``X``'s dimension is the same as the tuple ``dims``."""
    shape = tuple([int(d) for d in X.get_shape()])
    return shape == dims

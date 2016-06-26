from __future__ import print_function
import numpy as np
import tensorflow as tf

from edward.data import Data
from edward.models import Variational, PointMass
from edward.util import get_session, hessian, kl_multivariate_normal, log_sum_exp

try:
    import prettytensor as pt
except ImportError:
    pass

class Inference:
    """Base class for Edward inference methods.
    """
    def __init__(self, model, data=Data()):
        """Initialization.

        Calls ``util.get_session()``
        
        Parameters
        ----------
        model : ed.Model
            probability model
        data : ed.Data, optional
            observed data
        """
        self.model = model
        self.data = data
        get_session()

class MonteCarlo(Inference):
    """Base class for Monte Carlo inference methods.
    """
    def __init__(self, *args, **kwargs):
        """Initialization.

        Parameters
        ----------
        model : ed.Model
            probability model
        data : ed.Data, optional
            observed data
        """
        Inference.__init__(self, *args, **kwargs)

class VariationalInference(Inference):
    """Base class for variational inference methods.
    """
    def __init__(self, model, variational, data=Data()):
        """Initialization.

        Parameters
        ----------
        model : ed.Model
            probability model
        variational : ed.Variational
            variational model or distribution
        data : ed.Data, optional
            observed data        
        """
        Inference.__init__(self, model, data)
        self.variational = variational

    def run(self, *args, **kwargs):
        """A simple wrapper to run variational inference.

        1. Initialize via ``initialize``
        2. Run ``update`` for ``self.n_iter`` iterations 
        3. While running, ``print_progress``
        4. Finalize via ``finalize``

        Parameters
        ----------
        *args :
            goes nowhere
        **kwargs :
            passed into ``initialize``
        """
        self.initialize(*args, **kwargs)
        for t in range(self.n_iter+1):
            loss = self.update()
            self.print_progress(t, loss)
        self.finalize()

    def initialize(self, n_iter=1000, n_data=None, n_print=100,
        optimizer=None, scope=None):
        """Initialize variational inference algorithm.

        Set up ``tf.train.AdamOptimizer`` with a decaying scale factor.

        Initialize all variables

        Parameters
        ----------
        n_iter : int, optional
            Number of iterations for optimization.
        n_data : int, optional
            Number of samples for data subsampling. Default is to use all
            the data.
        n_print : int, optional
            Number of iterations for each print progress. To suppress print
            progress, then specify None.
        optimizer : str, optional
            Whether to use TensorFlow optimizer or PrettyTensor
            optimizer when using PrettyTensor. Defaults to TensorFlow.
        scope : str, optional
            Scope of TensorFlow variable objects to optimize over.
        """
        self.n_iter = n_iter
        self.n_data = n_data
        self.n_print = n_print

        self.loss = tf.constant(0.0)

        loss = self.build_loss()
        if optimizer is None:
            var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                         scope=scope)
            # Use ADAM with a decaying scale factor
            global_step = tf.Variable(0, trainable=False)
            starter_learning_rate = 0.1
            learning_rate = tf.train.exponential_decay(starter_learning_rate,
                                                global_step,
                                                100, 0.9, staircase=True)
            optimizer = tf.train.AdamOptimizer(learning_rate)
            self.train = optimizer.minimize(loss, global_step=global_step,
                                            var_list=var_list)
        else:
            if scope is not None:
                raise NotImplementedError("PrettyTensor optimizer does not accept a variable scope.")

            optimizer = tf.train.AdamOptimizer(0.01, epsilon=1.0)
            self.train = pt.apply_optimizer(optimizer, losses=[loss])

        init = tf.initialize_all_variables()
        init.run()

    def update(self):
        """Run one iteration of optimizer for variational inference.
    
        Returns
        -------
        loss : double
            variational `loss function` values after one iteration
        """
        sess = get_session()
        _, loss = sess.run([self.train, self.loss])
        return loss

    def print_progress(self, t, loss):
        """Print progress to output.
        
        Parameters
        ----------
        t : int
            Iteration counter
        loss : double
            Loss function value at iteration ``t``
        """
        if self.n_print is not None:
            if t % self.n_print == 0:
                print("iter {:d} loss {:.2f}".format(t, loss))
                print(self.variational)

    def finalize(self):
        """Finalize.

        Empty method. (Optional.)

        Any class based on ``VariationalInference`` **may** 
        implement this method.
        """
        pass

    def build_loss(self):
        """Build loss function. 

        Empty method. 

        Any class based on ``VariationalInference`` **must** 
        implement this method.

        Raises
        ------
        NotImplementedError
        """
        raise NotImplementedError()

# TODO this isn't MFVI so much as VI where q is analytic
class MFVI(VariationalInference):
    """Mean-field variational inference.

    This class implements a variety of "black-box" variational inference
    techniques (Ranganath et al., 2014) that maximize

    .. math:: 

        ELBO =  E_{q(z; \lambda)} [ \log p(x, z) - \log q(z; \lambda) ]
    """
    def __init__(self, *args, **kwargs):
        VariationalInference.__init__(self, *args, **kwargs)

    def initialize(self, n_minibatch=1, score=None, *args, **kwargs):
        """Initialization.

        Parameters
        ----------
        n_minibatch : int, optional
            Number of samples from variational model for calculating
            stochastic gradients.
        score : bool, optional
            Whether to force inference to use the score function
            gradient estimator. Otherwise default is to use the
            reparameterization gradient if available.
        """
        if score is None and self.variational.is_reparam:
            self.score = False
        else:
            self.score = True

        self.n_minibatch = n_minibatch
        return VariationalInference.initialize(self, *args, **kwargs)

    def update(self):
        """Runs one iteration of MFVI.

        Returns
        -------
        loss : double
            MFVI loss function value after one iteration.
        """
        sess = get_session()
        feed_dict = self.variational.np_sample(self.samples, self.n_minibatch)
        _, loss = sess.run([self.train, self.loss], feed_dict)
        return loss

    def build_loss(self):
        """Wrapper for the MFVI loss function.

        .. math:: 

            -ELBO =  -E_{q(z; \lambda)} [ \log p(x, z) - \log q(z; \lambda) ]

        MFVI supports

        1. score function gradients
        2. reparameterization gradients

        of the loss function.

        If the variational model is a Gaussian distribution, then part of the
        loss function can be computed analytically.  

        Returns
        -------
        result : 
            an appropriately selected loss function form
        """
        if self.score:
            if self.variational.is_normal and hasattr(self.model, 'log_lik'):
                return self.build_score_loss_kl()
            # Analytic entropies may lead to problems around
            # convergence; for now it is deactivated.
            #elif self.variational.is_entropy:
            #    return self.build_score_loss_entropy()
            else:
                return self.build_score_loss()
        else:
            if self.variational.is_normal and hasattr(self.model, 'log_lik'):
                return self.build_reparam_loss_kl()
            #elif self.variational.is_entropy:
            #    return self.build_reparam_loss_entropy()
            else:
                return self.build_reparam_loss()

    def build_score_loss(self):
        """Loss function to minimize. 

        Defines a stochastic gradient of
    
        .. math:: 

            -ELBO =  -E_{q(z; \lambda)} [ \log p(x, z) - \log q(z; \lambda) ]

        based on the score function estimator. (Paisley et al., 2012)

        Computed by sampling from :math:`q(z;\lambda)` and evaluating the 
        expectation using Monte Carlo sampling.
        """
        x = self.data.sample(self.n_data)
        z, self.samples = self.variational.sample(self.n_minibatch)

        q_log_prob = tf.zeros([self.n_minibatch], dtype=tf.float32)
        for i in range(self.variational.num_factors):
            q_log_prob += self.variational.log_prob_i(i, tf.stop_gradient(z))

        losses = self.model.log_prob(x, z) - q_log_prob
        self.loss = tf.reduce_mean(losses)
        return -tf.reduce_mean(q_log_prob * tf.stop_gradient(losses))

    def build_reparam_loss(self):
        """Loss function to minimize. 

        Defines a stochastic gradient of
    
        .. math:: 

            -ELBO =  -E_{q(z; \lambda)} [ \log p(x, z) - \log q(z; \lambda) ]

        based on the reparameterization trick. (Kingma and Welling, 2014)            

        Computed by sampling from :math:`q(z;\lambda)` and evaluating the 
        expectation using Monte Carlo sampling.
        """
        x = self.data.sample(self.n_data)
        z, self.samples = self.variational.sample(self.n_minibatch)

        q_log_prob = tf.zeros([self.n_minibatch], dtype=tf.float32)
        for i in range(self.variational.num_factors):
            q_log_prob += self.variational.log_prob_i(i, z)

        self.loss = tf.reduce_mean(self.model.log_prob(x, z) - q_log_prob)
        return -self.loss

    def build_score_loss_kl(self):
        """Loss function to minimize. 

        Defines a stochastic gradient of 
    
        .. math:: 

            -ELBO =  - ( E_{q(z; \lambda)} [ \log p(x | z) ]
                         + KL(q(z; \lambda) || p(z)) )

        based on the score function estimator. (Paisley et al., 2012)                         

        It assumes the KL is analytic.

        It assumes the prior is :math:`p(z) = \mathcal{N}(z; 0, 1)`

        Computed by sampling from :math:`q(z;\lambda)` and evaluating the 
        expectation using Monte Carlo sampling.
        """
        x = self.data.sample(self.n_data)
        z, self.samples = self.variational.sample(self.n_minibatch)

        q_log_prob = tf.zeros([self.n_minibatch], dtype=tf.float32)
        for i in range(self.variational.num_factors):
            q_log_prob += self.variational.log_prob_i(i, tf.stop_gradient(z))

        p_log_lik = self.model.log_lik(x, z)
        mu = tf.pack([layer.loc for layer in self.variational.layers])
        sigma = tf.pack([layer.scale for layer in self.variational.layers])
        kl = kl_multivariate_normal(mu, sigma)
        self.loss = tf.reduce_mean(p_log_lik) - kl
        return -(tf.reduce_mean(q_log_prob * tf.stop_gradient(p_log_lik)) - kl)

    def build_score_loss_entropy(self):
        """Loss function to minimize. 

        Defines a stochastic gradient of
    
        .. math:: 

            -ELBO =  - ( E_{q(z; \lambda)} [ \log p(x, z) ]
                        + H(q(z; \lambda)) )

        based on the score function estimator. (Paisley et al., 2012)                    

        It assumes the entropy is analytic.

        Computed by sampling from :math:`q(z;\lambda)` and evaluating the 
        expectation using Monte Carlo sampling.
        """        
        x = self.data.sample(self.n_data)
        z, self.samples = self.variational.sample(self.n_minibatch)

        q_log_prob = tf.zeros([self.n_minibatch], dtype=tf.float32)
        for i in range(self.variational.num_factors):
            q_log_prob += self.variational.log_prob_i(i, tf.stop_gradient(z))

        p_log_prob = self.model.log_prob(x, z)
        q_entropy = self.variational.entropy()
        self.loss = tf.reduce_mean(p_log_prob) + q_entropy
        return -(tf.reduce_mean(q_log_prob * tf.stop_gradient(p_log_prob)) +
                 q_entropy)

    def build_reparam_loss_kl(self):
        """Loss function to minimize. 

        Defines a stochastic gradient of
    
        .. math:: 

            -ELBO =  - ( E_{q(z; \lambda)} [ \log p(x | z) ]
                        + KL(q(z; \lambda) || p(z)) )

        based on the reparameterization trick. (Kingma and Welling, 2014)                             

        It assumes the KL is analytic.

        It assumes the prior is :math:`p(z) = \mathcal{N}(z; 0, 1)`

        Computed by sampling from :math:`q(z;\lambda)` and evaluating the 
        expectation using Monte Carlo sampling.
        """        
        x = self.data.sample(self.n_data)
        z, self.samples = self.variational.sample(self.n_minibatch)

        mu = tf.pack([layer.loc for layer in self.variational.layers])
        sigma = tf.pack([layer.scale for layer in self.variational.layers])
        self.loss = tf.reduce_mean(self.model.log_lik(x, z)) - \
                    kl_multivariate_normal(mu, sigma)
        return -self.loss

    def build_reparam_loss_entropy(self):
        """Loss function to minimize. 

        Defines a stochastic gradient of   
    
        .. math:: 

            -ELBO =  -( E_{q(z; \lambda)} [ \log p(x , z) ]
                        + H(q(z; \lambda)) )

        based on the reparameterization trick. (Kingma and Welling, 2014)                         

        It assumes the entropy is analytic.

        Computed by sampling from :math:`q(z;\lambda)` and evaluating the 
        expectation using Monte Carlo sampling.
        """ 
        x = self.data.sample(self.n_data)
        z, self.samples = self.variational.sample(self.n_minibatch)
        self.loss = tf.reduce_mean(self.model.log_prob(x, z)) + \
                    self.variational.entropy()
        return -self.loss

class KLpq(VariationalInference):
    """Variational inference that minimizes the Kullback-Leibler divergence 
    from the posterior to the variational model (Cappe et al., 2008)

    .. math::

        KL( p(z |x) || q(z) ).
    """
    def __init__(self, *args, **kwargs):
        VariationalInference.__init__(self, *args, **kwargs)

    def initialize(self, n_minibatch=1, *args, **kwargs):
        """Initialization.

        Parameters
        ----------
        n_minibatch : int, optional
            Number of samples from variational model for calculating
            stochastic gradients.
        """        
        self.n_minibatch = n_minibatch
        return VariationalInference.initialize(self, *args, **kwargs)

    def update(self):
        """Runs one iteration of KLpq minimization.

        Returns
        -------
        loss : double
            KLpq loss function value after one iteration.
        """        
        sess = get_session()
        feed_dict = self.variational.np_sample(self.samples, self.n_minibatch)
        _, loss = sess.run([self.train, self.loss], feed_dict)
        return loss

    def build_loss(self):
        """Loss function to minimize. 

        Defines a stochastic gradient of   
    
        .. math:: 
            KL( p(z |x) || q(z) )

        based on importance sampling.                         

        Computed as

        .. math::
            E_{q(z; \lambda)} [ w_{norm}(z; \lambda) *
                          ( \log p(x, z) - \log q(z; \lambda) ) ]

        where
    
        .. math::

            w_{norm}(z; \lambda) = w(z; \lambda) / \sum_z( w(z; \lambda) )

            w(z; \lambda) = p(x, z) / q(z; \lambda)

        which gives a gradient

        .. math::
            - E_{q(z; \lambda)} [ w_{norm}(z; \lambda) *
                                 \partial_{\lambda} \log q(z; \lambda) ]

        """         
        x = self.data.sample(self.n_data)
        z, self.samples = self.variational.sample(self.n_minibatch)

        q_log_prob = tf.zeros([self.n_minibatch], dtype=tf.float32)
        for i in range(self.variational.num_factors):
            q_log_prob += self.variational.log_prob_i(i, z)

        # 1/B sum_{b=1}^B grad_log_q * w_norm
        # = 1/B sum_{b=1}^B grad_log_q * exp{ log(w_norm) }
        log_w = self.model.log_prob(x, z) - q_log_prob

        # normalized log importance weights
        log_w_norm = log_w - log_sum_exp(log_w)
        w_norm = tf.exp(log_w_norm)

        self.loss = tf.reduce_mean(w_norm * log_w)
        return -tf.reduce_mean(q_log_prob * tf.stop_gradient(w_norm))

class MAP(VariationalInference):
    """Maximum a posteriori inference.

    We implement this using a ``PointMass`` variational distribution to 
    solve the following optimization problem

    .. math::

        \min_{z} - \log p(x,z)
    """
    def __init__(self, model, data=Data(), params=None):
        if hasattr(model, 'num_vars'):
            variational = Variational()
            variational.add(PointMass(model.num_vars, params))
        else:
            variational = Variational()
            variational.add(PointMass(0))

        VariationalInference.__init__(self, model, variational, data)

    def build_loss(self):
        """Loss function to minimize. 

        Defines the gradient of   
    
        .. math:: 
            - \log p(x,z)
        """         
        x = self.data.sample(self.n_data)
        z, _ = self.variational.sample()
        self.loss = tf.squeeze(self.model.log_prob(x, z))
        return -self.loss

class Laplace(VariationalInference):
    """Laplace approximation via Maximum a posteriori inference.

    We implement this using a ``PointMass`` variational distribution to 
    solve the following optimization problem

    .. math::

        \min_{z} - \log p(x,z)

    We then compute the hessian at the solution of the above problem. 
    (The mode of the posterior.)
    """
    def __init__(self, model, data=Data(), params=None):
        with tf.variable_scope("variational"):
            variational = Variational()
            variational.add(PointMass(model.num_vars, params))

        VariationalInference.__init__(self, model, variational, data)

    def build_loss(self):
        """Loss function to minimize. 

        Defines the gradient of   
    
        .. math:: 
            - \log p(x,z)
        """              
        x = self.data.sample(self.n_data)
        z, _ = self.variational.sample()
        self.loss = tf.squeeze(self.model.log_prob(x, z))
        return -self.loss

    def finalize(self):
        """Function to call after convergence.

        Computes the hessian at the mode.
        """              
        get_session()
        x = self.data.sample(self.n_data) # uses mini-batch
        z, _ = self.variational.sample()
        var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                     scope='variational')
        inv_cov = hessian(self.model.log_prob(x, z), var_list)
        print("Precision matrix:")
        print(inv_cov.eval())

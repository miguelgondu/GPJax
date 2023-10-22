# ---
# jupyter:
#   jupytext:
#     cell_metadata_filter: -all
#     custom_cell_magics: kql
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.11.2
#   kernelspec:
#     display_name: gpjax
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Regression with multiple outputs (EXPERIMENTAL)
#
# In this notebook we demonstate how to fit a Gaussian process regression model with multiple correlated outputs.
# This feature is still experimental.

# %%
# Enable Float64 for more stable matrix inversions.
from jax.config import config

config.update("jax_enable_x64", True)

from jax import jit
import jax.numpy as jnp
import jax.random as jr
from jaxtyping import install_import_hook
import matplotlib as mpl
import matplotlib.pyplot as plt
import optax as ox
from docs.examples.utils import clean_legend

# with install_import_hook("gpjax", "beartype.beartype"):
import gpjax as gpx

key = jr.PRNGKey(123)
# plt.style.use(
#    "https://raw.githubusercontent.com/JaxGaussianProcesses/GPJax/main/docs/examples/gpjax.mplstyle"
# )
cols = mpl.rcParams["axes.prop_cycle"].by_key()["color"]

# %% [markdown]
# ## Dataset
#
# With the necessary modules imported, we simulate a dataset
# $\mathcal{D} = (\boldsymbol{x}, \boldsymbol{y}) = \{(x_i, y_i)\}_{i=1}^{100}$ with inputs $\boldsymbol{x}$
# sampled uniformly on $(-3., 3)$ and corresponding independent noisy outputs
#
# $$\boldsymbol{y} \sim \mathcal{N} \left(\left[\sin(4\boldsymbol{x}) + \cos(2 \boldsymbol{x}), \sin(4\boldsymbol{x}) + \cos(3 \boldsymbol{x})\right], \textbf{I} * 0.3^2 \right).$$
#
# We store our data $\mathcal{D}$ as a GPJax `Dataset` and create test inputs and labels
# for later.

# %%
n = 100
noise = 0.3

key, subkey = jr.split(key)
x = jr.uniform(key=key, minval=-3.0, maxval=3.0, shape=(n,)).reshape(-1, 1)
f = lambda x: jnp.sin(4 * x) + jnp.array([jnp.cos(2 * x), jnp.cos(3 * x)]).T.squeeze()
signal = f(x)
y = signal + jr.normal(subkey, shape=signal.shape) * noise

D = gpx.Dataset(X=x, y=y)

xtest = jnp.linspace(-3.5, 3.5, 500).reshape(-1, 1)
ytest = f(xtest)

# %% [markdown]
# To better understand what we have simulated, we plot both the underlying latent
# function and the observed data that is subject to Gaussian noise.

# %%
fig, ax = plt.subplots(nrows=2, figsize=(7.5, 5))
for i in range(2):
    ax[i].plot(x, y[:, i], "x", label="Observations", color=cols[0])
    ax[i].plot(xtest, ytest[:, i], "--", label="Latent function", color=cols[1])
    ax[i].legend(loc="best")

# %% [markdown]
# Our aim in this tutorial will be to reconstruct the latent function from our noisy
# observations $\mathcal{D}$ via Gaussian process regression. We begin by defining a
# Gaussian process prior in the next section.
#
# ## Defining the prior
#
# A zero-mean Gaussian process (GP) places a prior distribution over real-valued
# functions $f(\cdot)$ where
# $f(\boldsymbol{x}) \sim \mathcal{N}(0, \mathbf{K}_{\boldsymbol{x}\boldsymbol{x}})$
# for any finite collection of inputs $\boldsymbol{x}$.
#
# Here $\mathbf{K}_{\boldsymbol{x}\boldsymbol{x}}$ is the Gram matrix generated by a
# user-specified symmetric, non-negative definite kernel function $k(\cdot, \cdot')$
# with $[\mathbf{K}_{\boldsymbol{x}\boldsymbol{x}}]_{i, j} = k(x_i, x_j)$.
# The choice of kernel function is critical as, among other things, it governs the
# smoothness of the outputs that our GP can generate.
#
# For simplicity, we consider a radial basis function (RBF) kernel to model similarity of inputs:
# $$k_\mathrm{inp}(x_\mathrm{inp}, x_\mathrm{inp}') = \sigma^2 \exp\left(-\frac{\lVert x - x' \rVert_2^2}{2 \ell^2}\right),$$
# and a categorical kernel to model similarity of outputs:
# $$k_\mathrm{idx}(x_\mathrm{idx}, x_\mathrm{idx}') = G_{x_\mathrm{idx}, x_\mathrm{idx}'}.$$
# Here, $G$ is an explicit gram matrix and $x_\mathrm{idx}, x_\mathrm{idx}'$ are indices to the output dimension and to $G$.
# For example $G_{1,2}$ contains the covariance between output dimensions $1$ and $2$, as does $G_{2,1} = G_{1,2}$.
#
# The overall kernel then is defined as
# $$k([x_\mathrm{inp}, x_\mathrm{idx}], [x_\mathrm{inp}', x_\mathrm{idx}']) = k_\mathrm{inp}(x_\mathrm{inp}, x_\mathrm{inp}') k_\mathrm{idx}(x_\mathrm{idx}, x_\mathrm{idx}').$$
# In the standard GPJax implementation, we never explicitly handle output dimension indices such as $x_\mathrm{idx}$.
# Rather, we simply define a dataset with multiple output columns.
#
# On paper a GP is written as $f(\cdot) \sim \mathcal{GP}(\textbf{0}, k(\cdot, \cdot'))$,
# we can reciprocate this process in GPJax via defining a `Prior` with our chosen `RBF`
# kernel.

# %%
kernel = gpx.kernels.RBF()
catkernel_params = gpx.kernels.CatKernel.gram_to_stddev_cholesky_lower(jnp.eye(2))
out_kernel = gpx.kernels.CatKernel(
    stddev=catkernel_params.stddev, cholesky_lower=catkernel_params.cholesky_lower
)
# out_kernel = gpx.kernels.White(variance=1.0)
meanf = gpx.mean_functions.Constant(jnp.array([0.0, 1.0]))
prior = gpx.Prior(mean_function=meanf, kernel=kernel, out_kernel=out_kernel)

# %% [markdown]
#
# The above construction forms the foundation for GPJax's models. Moreover, the GP prior
# we have just defined can be represented by a
# [TensorFlow Probability](https://www.tensorflow.org/probability/api_docs/python/tfp/substrates/jax)
# multivariate Gaussian distribution. Such functionality enables trivial sampling, and
# the evaluation of the GP's mean and covariance .

# %%
prior_dist = prior.predict(xtest)

prior_mean = prior_dist.mean()
prior_std = prior_dist.variance()
samples = prior_dist.sample(seed=key, sample_shape=(20,))


fig, ax = plt.subplots(nrows=2, figsize=(7.5, 5))
for i in range(2):
    ax[i].plot(xtest, samples.T[i], alpha=0.5, color=cols[0], label="Prior samples")
    ax[i].plot(xtest, prior_mean[:, i], color=cols[1], label="Prior mean")
    ax[i].fill_between(
        xtest.flatten(),
        prior_mean[:, i] - prior_std[:, i],
        prior_mean[:, i] + prior_std[:, i],
        alpha=0.3,
        color=cols[1],
        label="Prior variance",
    )
    ax[i].legend(loc="best")
    ax[i] = clean_legend(ax[i])

# %% [markdown]
# ## Constructing the posterior
#
# Having defined our GP, we proceed to define a description of our data
# $\mathcal{D}$ conditional on our knowledge of $f(\cdot)$ --- this is exactly the
# notion of a likelihood function $p(\mathcal{D} | f(\cdot))$. While the choice of
# likelihood is a critical in Bayesian modelling, for simplicity we consider a
# Gaussian with noise parameter $\alpha$
# $$p(\mathcal{D} | f(\cdot)) = \mathcal{N}(\boldsymbol{y}; f(\boldsymbol{x}), \textbf{I} \alpha^2).$$
# This is defined in GPJax through calling a `Gaussian` instance.

# %%
likelihood = gpx.Gaussian(num_datapoints=D.n)

# %% [markdown]
# The posterior is proportional to the prior multiplied by the likelihood, written as
#
#   $$ p(f(\cdot) | \mathcal{D}) \propto p(f(\cdot)) * p(\mathcal{D} | f(\cdot)). $$
#
# Mimicking this construct, the posterior is established in GPJax through the `*` operator.

# %%
posterior = prior * likelihood

# %% [markdown]
# <!-- ## Hyperparameter optimisation
#
# Our kernel is parameterised by a length-scale $\ell^2$ and variance parameter
# $\sigma^2$, while our likelihood controls the observation noise with $\alpha^2$.
# Using Jax's automatic differentiation module, we can take derivatives of  -->
#
# ## Parameter state
#
# As outlined in the [PyTrees](https://jax.readthedocs.io/en/latest/pytrees.html)
# documentation, parameters are contained within the model and for the leaves of the
# PyTree. Consequently, in this particular model, we have three parameters: the
# kernel lengthscale, kernel variance and the observation noise variance. Whilst
# we have initialised each of these to 1, we can learn Type 2 MLEs for each of
# these parameters by optimising the marginal log-likelihood (MLL).

# %%
negative_mll = gpx.objectives.ConjugateMLL(negative=True)
negative_mll(posterior, train_data=D)


# static_tree = jax.tree_map(lambda x: not(x), posterior.trainables)
# optim = ox.chain(
#     ox.adam(learning_rate=0.01),
#     ox.masked(ox.set_to_zero(), static_tree)
#     )
# %% [markdown]
# For researchers, GPJax has the capacity to print the bibtex citation for objects such
# as the marginal log-likelihood through the `cite()` function.

# %%
print(gpx.cite(negative_mll))

# %% [markdown]
# JIT-compiling expensive-to-compute functions such as the marginal log-likelihood is
# advisable. This can be achieved by wrapping the function in `jax.jit()`.

# %%
negative_mll = jit(negative_mll)

# %% [markdown]
# Since most optimisers (including here) minimise a given function, we have realised
# the negative
# marginal log-likelihood and just-in-time (JIT) compiled this to
# accelerate training.

# %% [markdown]
# We can now define an optimiser with `scipy`. For this example we'll use the `BFGS`
# optimiser.

# %%
opt_posterior, history = gpx.fit_scipy(
    model=posterior,
    objective=negative_mll,
    train_data=D,
)

# %% [markdown]
# ## Prediction
#
# Equipped with the posterior and a set of optimised hyperparameter values, we are now
# in a position to query our GP's predictive distribution at novel test inputs. To do
# this, we use our defined `posterior` and `likelihood` at our test inputs to obtain
# the predictive distribution as a multivariate Gaussian upon which `mean`
# and `stddev` can be used to extract the predictive mean and standard deviatation.

# %%
latent_dist = opt_posterior.predict(xtest, train_data=D)
predictive_dist = opt_posterior.likelihood(latent_dist)

predictive_mean = predictive_dist.mean()
predictive_std = predictive_dist.stddev()

# %% [markdown]
# With the predictions and their uncertainty acquired, we illustrate the GP's
# performance at explaining the data $\mathcal{D}$ and recovering the underlying
# latent function of interest.

# %%

fig, ax = plt.subplots(nrows=2, figsize=(7.5, 5))
for i in range(2):
    ax[i].plot(x, y[:, i], "x", label="Observations", color=cols[0], alpha=0.5)

    ax[i].fill_between(
        xtest.squeeze(),
        predictive_mean[:, i] - 2 * predictive_std[:, i],
        predictive_mean[:, i] + 2 * predictive_std[:, i],
        alpha=0.2,
        label="Two sigma",
        color=cols[1],
    )
    ax[i].plot(
        xtest,
        predictive_mean[:, i] - 2 * predictive_std[:, i],
        linestyle="--",
        linewidth=1,
        color=cols[1],
    )
    ax[i].plot(
        xtest,
        predictive_mean[:, i] + 2 * predictive_std[:, i],
        linestyle="--",
        linewidth=1,
        color=cols[1],
    )
    ax[i].plot(
        xtest,
        ytest[:, i],
        label="Latent function",
        color=cols[0],
        linestyle="--",
        linewidth=2,
    )
    ax[i].plot(xtest, predictive_mean[:, i], label="Predictive mean", color=cols[1])
    ax[i].legend(loc="center left", bbox_to_anchor=(0.975, 0.5))

# %% [markdown]
# ## System configuration

# %%
# %reload_ext watermark
# %watermark -n -u -v -iv -w -a 'Thomas Pinder & Daniel Dodd'

# %%

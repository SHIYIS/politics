# -*- coding: utf-8 -*-
import jax
from jax import jit
import jax.numpy as jnp
import pandas as pd
import numpy as np
from jax import random
import pandas as pd
import numpy as np
from scipy import sparse
import os
from numpyro import plate, sample, param
import numpyro.distributions as dist
from numpyro.distributions import constraints
from sklearn.decomposition import NMF

num_topics = 50
rng_seed = random.PRNGKey(0)

custom = 'tbip/data/custom_data/clean/author_map.txt'
if not os.path.isfile(custom):
    dataPath = 'tbip/data/senate-speeches-114/clean/'
else:
    dataPath = '/'.join(custom.split('/')[:-1])
# Load data
author_indices = jax.device_put(jnp.load(dataPath + "author_indices.npy"), jax.devices("gpu")[0])

counts = sparse.load_npz(dataPath + "counts.npz")

with open(dataPath + "vocabulary.txt",'r') as f:
    vocabulary = f.readlines()

with open(dataPath + "author_map.txt",'r') as f:
    author_map = f.readlines()

author_map = np.array(author_map)
num_authors = int(author_indices.max() + 1)
num_documents, num_words = counts.shape
pre_initialize_parameters = True

# Fit NMF to be used as initialization for TBIP

if pre_initialize_parameters:
  nmf_model = NMF(n_components=num_topics,
                  init='random',
                  random_state=0,
                  max_iter=500)
  # Define initialization arrays
  initial_document_loc = jnp.log(jnp.array(np.float32(nmf_model.fit_transform(counts) + 1e-2)))
  initial_objective_topic_loc = jnp.log(jnp.array(np.float32(nmf_model.components_ + 1e-2)))
else:
  rng1, rng2 = random.split(rng_seed, 2)
  initial_document_loc = random.normal(rng1,shape = (num_documents, num_topics))
  initial_objective_topic_loc = random.normal(rng2, shape =(num_topics, num_words))



# Define the model and variational family

class TBIP:
    def __init__(self, N, D, K, V, batch_size, init_mu_theta = None, init_mu_beta = None):
        self.N = N # number of people
        self.D = D # number of documents
        self.K = K # number of topics
        self.V = V # number of words in vocabulary
        self.batch_size = batch_size # number of documents in a batch

        if init_mu_theta is None:
            init_mu_theta = jnp.zeros([D, K])
        else:
            self.init_mu_theta = init_mu_theta

        if init_mu_beta is None:
            init_mu_beta = jnp.zeros([K, V])
        else:
            self.init_mu_beta = init_mu_beta

    def model(self, Y_batch, d_batch, i_batch):

        with plate("i", self.N):
            # Sample the per-unit latent variables (ideal points)
            x = sample("x", dist.Normal())

        with plate("k", size = self.K, dim = -2):
            with plate("k_v", size = self.V, dim = -1):
                beta = sample("beta", dist.Gamma(0.3, 0.3))
                eta = sample("eta", dist.Normal())

        with plate("d", size = self.D, subsample_size=self.batch_size, dim = -2):
            with plate("d_k", size = self.K, dim = -1):
                # Sample document-level latent variables (topic intensities)
                theta = sample("theta", dist.Gamma(0.3, 0.3))

            # Compute Poisson rates for each word
            P = jnp.sum(jnp.expand_dims(theta, 2) * jnp.expand_dims(beta, 0) *
                jnp.exp(jnp.expand_dims(x[i_batch], (1,2)) * jnp.expand_dims(eta, 0)), 1)

            with plate("v", size = self.V, dim = -1):
                # Sample observed words
                sample("Y_batch", dist.Poisson(P), obs = Y_batch)

    def guide(self, Y_batch, d_batch, i_batch):
        # This defines variational family. Notice that each of the latent variables
        # defined in the sample statements in the model above has a corresponding
        # sample statement in the guide. The guide is responsible for providing
        # variational parameters for each of these latent variables.

        # Also notice it is required that model and the guide have the same call.

        mu_x = param("mu_x", init_value = -1  + 2 * random.uniform(random.PRNGKey(1), (self.N,)))
        sigma_x = param("sigma_y", init_value = jnp.ones([self.N]), constraint  = constraints.positive)

        mu_eta = param("mu_eta", init_value = random.normal(random.PRNGKey(2), (self.K,self.V)))
        sigma_eta = param("sigma_eta", init_value = jnp.ones([self.K,self.V]), constraint  = constraints.positive)

        mu_theta = param("mu_theta", init_value =  self.init_mu_theta)
        sigma_theta = param("sigma_theta", init_value =  jnp.ones([self.D, self.K]), constraint  = constraints.positive)

        mu_beta = param("mu_beta", init_value = self.init_mu_beta)
        sigma_beta = param("sigma_beta", init_value = jnp.ones([self.K, self.V]), constraint  = constraints.positive)

        with plate("i", self.N):
            sample("x", dist.Normal(mu_x, sigma_x))

        with plate("k", size = self.K, dim = -2):
            with plate("k_v", size = self.V, dim = -1):
                sample("beta", dist.LogNormal(mu_beta, sigma_beta))
                sample("eta", dist.Normal(mu_eta, sigma_eta))

        with plate("d", size = self.D, subsample_size=self.batch_size, dim = -2):
            with plate("d_k", size = self.K, dim = -1):
                sample("theta", dist.LogNormal(mu_theta[d_batch], sigma_theta[d_batch]))

    def get_batch(self, rng, Y, author_indices):
        # Helper functions to obtain a batch of data, convert from scipy.sparse to jax.numpy.array and move to gpu
        D_batch = jax.random.choice(rng, jnp.arange(self.D), shape = (self.batch_size,))
        Y_batch = jax.device_put(jnp.array(Y[D_batch].toarray()), jax.devices("gpu")[0])
        D_batch = jax.device_put(D_batch, jax.devices("gpu")[0])
        I_batch = author_indices[D_batch]
        return Y_batch, I_batch, D_batch

"""## Initialization

Below we initialize an instance of the TBIP model, and associated SVI object. The latter is used to compute the Evidence Lower Bound (ELBO) given current value of guide's parameters and current batch of data.

We optimize the model using Adam with exponential decay of learning rate.
"""

# Initialize the model
from optax import adam, exponential_decay
from numpyro.infer import SVI, TraceMeanField_ELBO

num_steps = 50000
batch_size = 512

tbip = TBIP(
    N = num_authors,
    D = num_documents,
    K = num_topics,
    V = num_words,
    batch_size = batch_size,
    init_mu_theta = initial_document_loc,
    init_mu_beta = initial_objective_topic_loc)

svi_batch = SVI(
    model=tbip.model,
    guide=tbip.guide,
    optim = adam(exponential_decay(0.01, num_steps, 0.01)),
    loss = TraceMeanField_ELBO())

# Compile update function for faster training
svi_batch_update = jit(svi_batch.update)

# Get initial batch. This informs the dimension of arrays and ensures they are
# consistent with dimensions (N, D, K, V) defined above.
Y_batch, I_batch, D_batch = tbip.get_batch(random.PRNGKey(1), counts, author_indices)

# Initialize the parameters using initial batch
svi_state = svi_batch.init(
    random.PRNGKey(0),
    Y_batch = Y_batch,
    d_batch = D_batch,
    i_batch = I_batch)

#@title Run this cell to create helper functions for printing topics and ordered ideal points

def get_topics(neutral_mean,
               negative_mean,
               positive_mean,
               vocabulary,
               print_to_terminal=True):
  num_topics, num_words = neutral_mean.shape
  words_per_topic = 10
  top_neutral_words = np.argsort(-neutral_mean, axis=1)
  top_negative_words = np.argsort(-negative_mean, axis=1)
  top_positive_words = np.argsort(-positive_mean, axis=1)
  topic_strings = []
  for topic_idx in range(num_topics):
    neutral_start_string = "Neutral  {}:".format(topic_idx)
    neutral_row = [vocabulary[word] for word in
                    top_neutral_words[topic_idx, :words_per_topic]]
    neutral_row_string = ", ".join(neutral_row)
    neutral_string = " ".join([neutral_start_string, neutral_row_string])

    positive_start_string = "Positive {}:".format(topic_idx)
    positive_row = [vocabulary[word] for word in
                    top_positive_words[topic_idx, :words_per_topic]]
    positive_row_string = ", ".join(positive_row)
    positive_string = " ".join([positive_start_string, positive_row_string])

    negative_start_string = "Negative {}:".format(topic_idx)
    negative_row = [vocabulary[word] for word in
                    top_negative_words[topic_idx, :words_per_topic]]
    negative_row_string = ", ".join(negative_row)
    negative_string = " ".join([negative_start_string, negative_row_string])

    if print_to_terminal:
      topic_strings.append(negative_string)
      topic_strings.append(neutral_string)
      topic_strings.append(positive_string)
      topic_strings.append("==========")
    else:
      topic_strings.append("  \n".join(
        [negative_string, neutral_string, positive_string]))

  if print_to_terminal:
    all_topics = "{}\n".format(np.array(topic_strings))
  else:
    all_topics = np.array(topic_strings)
  return all_topics

"""## Execute Training
The code above was creating the model; below we actually run training. You can adjust the number of steps to train (num_steps, defined above) and the frequency at which to print the ELBO (print_steps, defined below).
"""

print_steps = 5000

"""Here, we run our training loop. Topic summaries and ordered ideal points will print every 2500 steps. Typically in our experiments it takes 15,000 steps or so to begin seeing sensible results, but of course this depends on the corpus. These sensible results should be reached within a half hour. For the default corpus of Senate speeches, it should take less than 2 hours to complete the full 50,000 training steps.

"""

# Run SVI
from tqdm import tqdm
from tbip.tbip import print_topics
rngs = random.split(random.PRNGKey(2), num_steps)
losses = []
pbar = tqdm(range(num_steps))

for step in pbar:
    Y_batch, I_batch, D_batch = tbip.get_batch(rngs[step], counts, author_indices)
    svi_state, loss = svi_batch_update(svi_state,
        Y_batch = Y_batch,
        d_batch = D_batch,
        i_batch = I_batch)

    loss = loss/counts.shape[0]
    losses.append(loss)
    if step%print_steps == 0 or step == num_steps - 1:
        pbar.set_description("Init loss: " + "{:10.4f}".format(jnp.array(losses[0])) +
         "; Avg loss (last 100 iter): " + "{:10.4f}".format(jnp.array(losses[-100:]).mean()))

    if (step + 1) % 2500 == 0 or step == num_steps - 1:

        print(f"Results after {step} steps.")
        estimated_params = svi_batch.get_params(svi_state)

        neutral_mean = estimated_params["mu_beta"] + estimated_params["sigma_beta"]**2/2

        positive_mean = (estimated_params["mu_beta"] + estimated_params["mu_eta"] +
            (estimated_params["sigma_beta"]**2 + estimated_params["sigma_eta"]**2 )/2)

        negative_mean = (estimated_params["mu_beta"] - estimated_params["mu_eta"] +
            (estimated_params["sigma_beta"]**2 + estimated_params["sigma_eta"]**2 )/2)

        np.save("neutral_topic_mean.npy", neutral_mean)
        np.save("negative_topic_mean.npy", positive_mean)
        np.save("positive_topic_mean.npy", negative_mean)

        topics = print_topics(neutral_mean,
                            positive_mean,
                            negative_mean,
                            vocabulary)

        with open('topics.txt', 'w') as f:
            print(topics, file=f)
        print(topics)

        authors = pd.DataFrame({"name": author_map, "ideal_point" :  estimated_params["mu_x"]})
        authors.to_csv("authors.csv")

        sorted_authors = "Authors sorted by their ideal points: " + \
          ",".join(list(authors.sort_values("ideal_point")["name"]))

        print(sorted_authors.replace("\n", " "))

from google.colab import drive
drive.mount('/content/drive')

import os
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
neutral_topic_mean = np.load("../output/c4fe2022-tweets/neutral_topic_mean.npy")
negative_topic_mean = np.load("../output/c4fe2022-tweets/negative_topic_mean.npy")
positive_topic_mean = np.load("../output/c4fe2022-tweets/positive_topic_mean.npy")
authors = pd.read_csv("authors.csv")
authors["name"] = authors["name"].str.replace("\n", "")

selected_authors = np.array(
    ["Dean Heller (R)", "Bernard Sanders (I)", "Elizabeth Warren (D)", "Charles Schumer (D)",
     "Susan Collins (R)", "Marco Rubio (R)", "John Mccain (R)",
     "Ted Cruz (R)"])

"""For example, here is a graph of the learned ideal points. We don't label each point because there are too many to plot. Below we select some authors to label."""

sns.set(style="whitegrid")
fig = plt.figure(figsize=(12, 1))
ax = plt.axes([0, 0, 1, 1], frameon=False)
for index in range(authors.shape[0]):
  ax.scatter(authors["ideal_point"][index], 0, c='black', s=20)
  if authors["name"][index] in selected_authors:
      ax.annotate(author_map[index],
                  xy=(authors["ideal_point"][index], 0.),
                  xytext=(authors["ideal_point"][index], 0), rotation=30, size=14)
ax.set_yticks([])

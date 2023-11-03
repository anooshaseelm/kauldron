# Copyright 2023 The kauldron Authors.
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

"""Default TrainState and TrainStep implementations."""
from __future__ import annotations

import dataclasses
from typing import Any, Mapping, Optional

from etils import epy
import flax
import flax.linen as nn
import jax
from kauldron import losses as kd_losses
from kauldron import metrics as kd_metrics
from kauldron import summaries as kd_summaries
import kauldron.data.utils as data_utils
from kauldron.train import rngs_lib
from kauldron.typing import ElementSpec, Float, PyTree  # pylint: disable=g-multiple-import,g-importing-member
from kauldron.utils import config_util
from kauldron.utils import core
from kauldron.utils import jax_utils
from kauldron.utils import train_property  # pylint: disable=unused-import
from kauldron.utils.sharding_utils import sharding  # pylint: disable=g-importing-member
import optax

_Params = PyTree[Float["..."]]


@flax.struct.dataclass
class TrainState:
  """Data structure for checkpointing the model."""

  _: dataclasses.KW_ONLY

  step: int

  params: Optional[_Params]
  opt_state: Optional[PyTree[Float["..."]]]

  training_time_hours: float

  def next(self, new_params=None, new_opt_state=None) -> TrainState:
    step = self.step + 1
    new_params = new_params or self.params
    new_opt_state = new_opt_state or self.opt_state
    return self.replace(
        step=step,
        params=new_params,
        opt_state=new_opt_state,
    )

  def replace(self, **changes: Any) -> TrainState:
    return dataclasses.replace(self, **changes)


@flax.struct.dataclass
class Auxiliaries:
  """Auxiliaries."""

  loss_states: dict[str, kd_metrics.State] = dataclasses.field(
      default_factory=dict
  )
  metric_states: dict[str, kd_metrics.State] = dataclasses.field(
      default_factory=dict
  )
  summary_kwargs: dict[str, Any] = dataclasses.field(default_factory=dict)

  def replace(self, **changes: Any) -> Auxiliaries:
    return dataclasses.replace(self, **changes)

  def merge(self, other: Auxiliaries) -> Auxiliaries:
    """Accumulate auxiliary."""
    # TODO(epot): How to merge summaries ?
    return self.replace(
        loss_states=_reduce_states(self.loss_states, other.loss_states),
        metric_states=_reduce_states(self.metric_states, other.metric_states),
    )


def _reduce_states_single(
    states: tuple[kd_metrics.State, ...]
) -> kd_metrics.State:
  final_state, *rest_states = states
  for state in rest_states:
    final_state = final_state.merge(state)
  return final_state


def _reduce_states(
    *all_states: dict[str, kd_metrics.State]
) -> dict[str, kd_metrics.State]:
  """Merge all the states from the different metrics."""
  return {
      k: _reduce_states_single(states)
      for k, states in epy.zip_dict(*all_states)
  }


@dataclasses.dataclass(kw_only=True, eq=True, frozen=True)
class ModelWithAux(config_util.UpdateFromRootCfg):
  """Wrapper around model which also compute the summaries and metrics."""

  model: nn.Module = config_util.ROOT_CFG_REF.model
  losses: Mapping[str, kd_losses.Loss] = config_util.ROOT_CFG_REF.train_losses
  metrics: Mapping[str, kd_metrics.Metric] = (
      config_util.ROOT_CFG_REF.train_metrics
  )
  summaries: Mapping[str, kd_summaries.Summary] = (
      config_util.ROOT_CFG_REF.train_summaries
  )

  def init(  # pylint:disable=missing-function-docstring
      self,
      init_rngs: rngs_lib.Rngs,
      elem_spec: ElementSpec,
      model_method: Optional[str] = None,
  ) -> _Params:
    self._assert_root_cfg_resolved()
    mock_batch = data_utils.mock_batch_from_elem_spec(elem_spec)
    context = core.Context(step=0, batch=mock_batch)
    args, kwargs = data_utils.get_model_inputs(self.model, context)
    params = self.model.init(
        init_rngs,
        *args,
        method=model_method,
        is_training_property=True,
        **kwargs,
    )["params"]
    params = flax.core.unfreeze(params)
    return params

  @jax.named_call
  def forward(
      self,
      params,
      *,
      batch,
      rngs: rngs_lib.Rngs,
      step: int,
      is_training: bool,
  ) -> tuple[float, core.Context]:
    """Forward pass of the model including losses."""
    context = core.Context(step=step, batch=batch, params=params)
    args, kwargs = data_utils.get_model_inputs(self.model, context)
    preds, intermediates = self.model.apply(  # TODO(klausg): capture mutables?
        {"params": params},
        *args,
        rngs=rngs,
        capture_intermediates=True,  # TODO(klausg): check if need a filter here
        is_training_property=is_training,
        **kwargs,
    )
    context = context.replace(
        preds=preds, interms=intermediates["intermediates"]
    )
    loss_total, loss_states = kd_losses.compute_losses(
        losses=self.losses, context=context
    )
    return loss_total, context.replace(loss_states=loss_states)

  @jax.named_call
  def get_aux(
      self,
      context: core.Context,
      *,
      # TODO(epot): Better signature
      return_losses: bool = False,
      return_metrics: bool = False,
      return_summaries: bool = False,
  ) -> Auxiliaries:
    """Get auxilaries."""
    aux = Auxiliaries()
    if return_losses:
      aux = aux.replace(loss_states=context.loss_states)

    if return_metrics:
      aux = aux.replace(
          metric_states=jax.tree_util.tree_map(
              lambda m: m.get_state_from_context(context), self.metrics
          )
      )

    if return_summaries:
      aux = aux.replace(
          summary_kwargs={
              k: summary.gather_kwargs(context)
              for k, summary in self.summaries.items()
          }
      )
    return aux


@dataclasses.dataclass(kw_only=True, eq=True, frozen=True)
class TrainStep(config_util.UpdateFromRootCfg):
  """Training Step."""

  model_with_aux: ModelWithAux = dataclasses.field(default_factory=ModelWithAux)
  optimizer: optax.GradientTransformation = config_util.ROOT_CFG_REF.optimizer
  rng_streams: rngs_lib.RngStreams = config_util.ROOT_CFG_REF.rng_streams

  def update_from_root_cfg(self, root_cfg) -> TrainStep:
    new_self = super().update_from_root_cfg(root_cfg)
    new_self = dataclasses.replace(
        new_self,
        model_with_aux=new_self.model_with_aux.update_from_root_cfg(root_cfg),
    )
    return new_self

  def init(
      self,
      elem_spec: ElementSpec,
      *,
      model_method: Optional[str] = None,
  ) -> TrainState:
    """Initialize the model and return the initial TrainState."""
    self._assert_root_cfg_resolved()
    return self._init(flax.core.freeze(elem_spec), model_method)

  @jax_utils.jit(
      out_shardings=lambda: sharding.REPLICATED,
      static_argnames=("self", "elem_spec", "model_method"),
  )
  def _init(
      self,
      elem_spec: ElementSpec,
      model_method: Optional[str] = None,
  ) -> TrainState:
    """Initialize the model and return the initial TrainState."""
    params = self.model_with_aux.init(
        self.rng_streams.init_rngs(), elem_spec, model_method=model_method
    )
    opt_state = self.optimizer.init(params)
    return TrainState(
        step=0,
        params=params,
        opt_state=opt_state,
        training_time_hours=0.0,
    )

  @jax_utils.jit(
      static_argnames=(
          "self",
          "return_losses",
          "return_metrics",
          "return_summaries",
      ),
      donate_argnames=("state",),
      # in_shardings=lambda: dict(  # pylint: disable=g-long-lambda
      #     state=sharding.REPLICATED,
      #     batch=sharding.SHARDED,
      # ),
      out_shardings=lambda: sharding.REPLICATED,
  )
  @jax.named_call
  def step(
      self,
      state: TrainState,
      batch: PyTree[Any],
      *,
      return_losses: bool = False,
      return_metrics: bool = False,
      return_summaries: bool = False,
  ) -> tuple[TrainState, Auxiliaries]:
    """Training step: forward, losses, gradients, update, and metrics."""
    # TODO(epot): Should `jax.named_call` be moved downstream directly in optax?

    # NOTE: ensure that evaluation metrics are computed from the OLD model state
    # *before* backprop gradients are applied.
    grad_fn = jax.grad(self.model_with_aux.forward, argnums=0, has_aux=True)
    grads, context = jax.named_call(grad_fn, name="grad_fn")(
        state.params,
        batch=batch,
        rngs=self.rng_streams.train_rngs(state.step),
        step=state.step,
        is_training=True,
    )
    updates, new_opt_state = jax.named_call(self.optimizer.update)(
        grads, state.opt_state, state.params
    )
    new_params = jax.named_call(optax.apply_updates)(state.params, updates)
    next_state = state.next(new_params=new_params, new_opt_state=new_opt_state)

    context = context.replace(grads=grads, updates=updates)

    aux = self.model_with_aux.get_aux(
        context,
        return_losses=return_losses,
        return_metrics=return_metrics,
        return_summaries=return_summaries,
    )

    return next_state, aux

# Copyright 2024 The kauldron Authors.
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

"""Test."""

import os

from etils import epath
from kauldron import kd
from examples import mnist_autoencoder


def test_sharding(tmp_path: epath.Path):
  # Load config and reduce size
  cfg = mnist_autoencoder.get_config()

  cfg.train_ds.batch_size = 2
  cfg.model.encoder.features = 3
  cfg.workdir = os.fspath(tmp_path)

  trainer = kd.konfig.resolve(cfg)

  # Get the state
  with kd.kmix.testing.mock_data():
    state = trainer.init_state()

  del state
  # TODO(epot): How to test this is actually working?
  # for k, v in kd.kontext.flatten_with_path(state).items():
  #   assert v.sharding == kd.sharding.REPLICATED, k

import importlib

import pytest


@pytest.mark.parametrize('name', [
    'blackbull.client.scenario',
    'blackbull.client.scenario_oracle',
])
def test_removed_client_scenario_modules_cannot_be_imported(name):
    with pytest.raises(ModuleNotFoundError) as caught:
        importlib.import_module(name)
    assert caught.value.name == name

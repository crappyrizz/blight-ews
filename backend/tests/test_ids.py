import re

import pytest

from backend import ids


def test_new_id_format():
    assert re.fullmatch(r"RD-[0-9A-F]{12}", ids.new_id(ids.SENSOR_READING))


def test_new_ids_are_unique():
    generated = {ids.new_id(ids.FARMER) for _ in range(10_000)}
    assert len(generated) == 10_000


def test_unknown_prefix_rejected():
    with pytest.raises(ValueError):
        ids.new_id("BAD")

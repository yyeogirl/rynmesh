"""Wire-byte compatibility when reusing canonical encoder options across calls."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from rynmesh.crypto import canonical_json


def legacy(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def test_canonical_wire_bytes_match_previous_encoder_under_concurrency():
    values = [None, False, True, 0, -0.0, 1.2345678901234567, 10**30, float('nan'), float('inf'),
              '中文\n\t"\\😀', [], {}, {'z': ['é', 1, None], 'a': {'reading': .2, 'saved': True}}]
    values.extend({'id': index, 'value': values[index % 13]} for index in range(100))
    expected = [legacy(value) for value in values]
    with ThreadPoolExecutor(max_workers=8) as pool:
        actual = list(pool.map(canonical_json, values * 3))
    assert actual == expected * 3
    assert canonical_json({'z': -0.0, 'a': 'é\n'}) == b'{"a":"\xc3\xa9\\n","z":-0.0}'


def test_failed_encoding_does_not_poison_later_calls():
    circular = []
    circular.append(circular)
    for value, error in ((circular, ValueError), ({'bad': object()}, TypeError), ('\ud800', UnicodeEncodeError)):
        with pytest.raises(error):
            canonical_json(value)
        with pytest.raises(error):
            legacy(value)
        assert canonical_json({'ok': True}) == b'{"ok":true}'

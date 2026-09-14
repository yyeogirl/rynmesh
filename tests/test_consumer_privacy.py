"""Result erasure retains real order identity and waits for active execution."""
import threading

import pytest

from rynmesh.llm_package.privacy import erase_consumer_results
from rynmesh.llm_package.task_protocol import TaskOrderStore, TaskProtocolError


def order(store, identifier, state='succeeded'):
    store.transition(task_id=identifier, state='created')
    if state == 'succeeded':
        store.transition(task_id=identifier, state='accepted')
        store.transition(task_id=identifier, state='running')
    store.transition(task_id=identifier, state=state, encrypted_response={'ciphertext': 'retained secret'})


def test_terminal_disk_and_memory_results_are_cleared_without_losing_order_identity(tmp_path):
    store = TaskOrderStore(tmp_path)
    order(store, 'task-one')
    pending = {'task-one': {'task_id': 'task-one', 'state': 'succeeded', 'output': 'private answer', 'ephemeral': True},
               'task-rejected': {'task_id': 'task-rejected', 'state': 'failed', 'detail': 'private provider detail'}}
    lock = threading.RLock()
    result = erase_consumer_results(store, pending, lock, ['task-one', 'task-rejected'])
    assert result['results_removed'] == 1
    saved = store.get('task-one')
    assert saved['state'] == 'succeeded' and 'encrypted_response' not in saved
    assert saved['history'][-1]['state'] == 'succeeded'
    assert pending['task-one'] == {'task_id': 'task-one', 'state': 'succeeded'}
    assert pending['task-rejected'] == {'task_id': 'task-rejected', 'state': 'failed'}
    assert erase_consumer_results(store, pending, lock, ['task-one'])['results_removed'] == 0


@pytest.mark.parametrize('memory_state', ['queued', 'running', 'future-state'])
def test_terminal_checkpoint_is_not_enough_while_background_task_is_still_active(tmp_path, memory_state):
    store = TaskOrderStore(tmp_path)
    order(store, 'task-one')
    pending = {'task-one': {'state': memory_state}}
    before = store._path('task-one').read_bytes()
    with pytest.raises(TaskProtocolError, match='ask_cleanup_orders_active'):
        erase_consumer_results(store, pending, threading.RLock(), ['task-one'])
    assert store._path('task-one').read_bytes() == before


def test_disk_active_record_blocks_batch_before_any_other_result_is_erased(tmp_path):
    store = TaskOrderStore(tmp_path)
    order(store, 'task-a')
    order(store, 'task-b', state='accepted')
    with pytest.raises(TaskProtocolError, match='ask_cleanup_orders_active'):
        erase_consumer_results(store, {}, threading.RLock(), ['task-a', 'task-b'])
    assert 'encrypted_response' in store.get('task-a')

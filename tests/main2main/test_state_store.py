from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from state_store import JsonStore


def test_json_store_load_returns_empty_dict_when_file_does_not_exist():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        assert store.load() == {}


def test_json_store_save_and_load_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        store.save({"key": "value", "nested": {"a": 1}})
        assert store.load() == {"key": "value", "nested": {"a": 1}}


def test_json_store_save_creates_parent_directories():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "sub" / "dir" / "state.json")
        store.save({"x": 1})
        assert store.load() == {"x": 1}


def test_json_store_locked_context_manager_provides_data_and_saves_on_exit():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        store.save({"counter": 0})
        with store.locked() as data:
            data["counter"] = 1
        assert store.load() == {"counter": 1}


def test_json_store_locked_serializes_concurrent_writes():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        store.save({"counter": 0})
        errors = []

        def increment():
            try:
                with store.locked() as data:
                    val = data["counter"]
                    data["counter"] = val + 1
            except Exception as exc:  # pragma: no cover - assertion below checks this
                errors.append(exc)

        threads = [threading.Thread(target=increment) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        assert store.load()["counter"] == 10

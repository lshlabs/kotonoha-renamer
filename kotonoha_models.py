"""Local Ollama model management. The caller holds the user-data lock."""

import kotonoha_setup as setup
import kotonoha_storage as storage


def label(name):
    prefix = setup.catalog()[0]["model"].rsplit(":", 1)[0] + ":"
    return "SuperGemma · " + name[len(prefix) :] if name.startswith(prefix) else name


def config():
    return storage.read_json(storage.DATA_DIR / "config.json", {"version": 2, "model": None})


def set_default(name):
    settings = config()
    settings["model"] = name
    storage.atomic_json(storage.DATA_DIR / "config.json", settings)


def installed(client):
    return sorted(client.list().models, key=lambda item: item.model.casefold())

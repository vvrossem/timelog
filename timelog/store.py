import datetime as dt
import json
import os
import shutil

from . import config
from . import model
from . import style


def day_path(date):
    return f"{config.DAY_DIR}/{date}.json"


def load_day(date):
    try:
        with open(day_path(date)) as f:
            data = json.load(f)
    except OSError:
        return None
    entries = [
        model.Entry(e["task"], dt.datetime.fromisoformat(e["start"]), dt.datetime.fromisoformat(e["end"]),
              e["source"], tuple(e.get("streams", ())), e.get("agent", 0))
        for e in data["entries"]
    ]
    return {"entries": entries, "comments": data.get("comments", {}), "defaults": data.get("defaults", {}),
            "agent_minutes": data.get("agent_minutes", {}),
            "pushed": data.get("pushed", []), "renames": data.get("renames", {}),
            "overrides": data.get("overrides", []), "breaks": data.get("breaks", []), "entered": data.get("entered"), "saved": data.get("saved")}


def save_day(date, day, quiet=False):
    os.makedirs(config.BACKUPS, exist_ok=True)
    path = day_path(date)
    if os.path.exists(path):
        shutil.copy2(path, f"{config.BACKUPS}/{date}-{dt.datetime.now():%Y%m%d-%H%M%S-%f}.json")
    data = {
        "entries": [
            {"task": e.task, "start": e.start.isoformat(), "end": e.end.isoformat(), "source": e.source,
             "streams": list(e.streams), "agent": e.agent}
            for e in day["entries"]
        ],
        "comments": day["comments"],
        "defaults": day.get("defaults", {}),  # comments suggested for the tasks without one, recomputed
        "pushed": day.get("pushed", []),
        "renames": day.get("renames", {}),
        "overrides": day.get("overrides", []),
        "breaks": day.get("breaks", []),  # [start, end] of the stretches with nothing active you said were breaks
        "entered": day.get("entered"),  # the worklogs you said you entered in Tempo, without Jira
        "saved": dt.datetime.now().astimezone().isoformat(),  # a copy of the file keeps it
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=1)
    if not quiet:
        style.note(f"written to {path}")

import os

SESSIONS_DIR = "sessions"
SCREENSHOTS_DIR = "screenshots"

INPUT_DIR = "input"


current_locals = locals().copy()
for varname, value in current_locals.items():
    if varname.isupper() and varname.endswith("DIR"):
        os.makedirs(value, exist_ok=True)

    if varname.isupper() and varname.endswith("FILE"):
        if not os.path.exists(value):
            with open(value, "w", encoding="UTF-8") as _:
                pass

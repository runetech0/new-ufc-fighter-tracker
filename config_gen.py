# type: ignore
import os
from pathlib import Path
from typing import TypeVar

import toml
from tomlkit import dumps, item, parse
from tomlkit.items import AoT, Array, Bool, Float, InlineTable, Integer, String, Table

# Type variable for type-safe dynamic class creation
T = TypeVar("T")


def _placeholder(value):
    """Return an ‘empty’ value of the same TOML type."""
    if isinstance(value, String):
        return item("")  # ""
    if isinstance(value, Integer):
        return item(0)  # 0
    if isinstance(value, Float):
        return item(0.0)  # 0.0
    if isinstance(value, Bool):
        return item(False)  # false
    if isinstance(value, Array):
        # Keep same length & style, recurse element‑wise
        for i, v in enumerate(value):
            value[i] = _placeholder(v)
        return value
    if isinstance(value, AoT):
        for table in value:
            _wipe(table)  # recurse into each table
        return value
    if isinstance(value, (Table, InlineTable)):
        _wipe(value)  # handled below
        return value
    # For dates, datetimes, times, etc. we simply leave them blank:
    return item("")  # "" for unsupported scalars


def _wipe(tbl):
    """Recursively replace all leaf values inside a table with placeholders."""
    for k, v in tbl.items():
        if isinstance(v, (Table, InlineTable, AoT, Array)):
            tbl[k] = _placeholder(v)
        else:
            tbl[k] = _placeholder(v)


def make_empty_toml(src: str | Path, dst: str | Path | None = None) -> Path:
    """
    Read *src* TOML, create a copy with empty/default values, and
    write it to *dst* (defaults to `<name>_empty.toml` beside the source).
    Comments, whitespace, and ordering are preserved.
    """
    src = Path(src)
    if dst is None:
        dst = src.with_stem(src.stem + "_empty")
    with src.open(encoding="utf‑8") as f:
        doc = parse(f.read())

    _wipe(doc)

    with Path(dst).open("w", encoding="utf‑8") as f:
        f.write(dumps(doc))

    return Path(dst)


# Define the config file path
_CONFIG_FILE = "config.toml"
_SAMPLE_CONFIG_FILE = "sample-config.toml"
make_empty_toml(_CONFIG_FILE, _SAMPLE_CONFIG_FILE)
_CONFIG_DATA = toml.load(_CONFIG_FILE)  # Load TOML data


def detect_type(value: object) -> str:
    """Returns the Python type as a string for strict typing."""
    if isinstance(value, bool):
        return "bool"
    elif isinstance(value, int):
        return "int"
    elif isinstance(value, float):
        return "float"
    elif isinstance(value, str):
        return "str"
    elif isinstance(value, list):
        return "list"
    elif isinstance(value, dict):
        return "dict"
    return "object"  # Fallback for unknown types


def generate_section_class(name: str, values: dict) -> str:
    """Generates a dataclass for a TOML section with strict typing."""
    class_code = f"@dataclass\nclass {name}:\n"
    for key, value in values.items():
        attr_type = detect_type(value)
        if attr_type == "str":
            value = ""
        class_code += f"    {key}: {attr_type} = {repr(value)}\n"
    class_code += "\n"
    return class_code


def generate_config_class(toml_data: dict) -> str:
    """Generates the main Config class with typed attributes."""
    class_code = "class Config:\n"
    for section in toml_data.keys():
        class_code += f"    {section}: '{section}'\n"
    class_code += "\n"

    # Auto-load function
    class_code += "    @classmethod\n"
    class_code += "    def load(cls) -> None:\n"
    for section, values in toml_data.items():
        class_code += f"        cls.{section} = {section}(\n"
        for key in values.keys():
            class_code += f"            {key}=_CONFIG_DATA['{section}']['{key}'],\n"
        class_code = class_code.rstrip(",\n") + "\n        )\n"
    class_code += "\nConfig.load()\n"

    return class_code


# Generate section classes
generated_code = "from dataclasses import dataclass\n\n"

# 🔥 Add `_CONFIG_DATA` definition inside the generated file
generated_code += "import toml\n"
generated_code += "import os, sys\n\n"
generated_code += "_CONFIG_FILE = 'config.toml'\n"
generated_code += '''
if not os.path.exists(_CONFIG_FILE):
    print(
        """❌ config.toml file is missing.
          👉 Please rename 'sample-config.toml' to 'config.toml'
          or ask the developer to send you 'sample-config.toml' file."""
    )
    input("Press 'Enter' 3-times to exit...")
    input("Press 'Enter' 2-times more...")
    input("Press 'Enter' to exit now...")
    sys.exit(1)
'''
generated_code += "\n_CONFIG_DATA = toml.load(_CONFIG_FILE)\n\n"

# Generate dataclasses for each section
for section, values in _CONFIG_DATA.items():
    generated_code += generate_section_class(section, values)

# Generate the main Config class
generated_code += generate_config_class(_CONFIG_DATA)

output_filename = "config_reader.py"
# Save to config_class.py
with open(output_filename, "w") as f:
    f.write(generated_code)

print(f"✅ Config class generated in {output_filename}")
os.rename(output_filename, f"app/{output_filename}")

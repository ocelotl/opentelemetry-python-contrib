from os import walk, getcwd
from os.path import join
from tomlkit import parse, dumps


pyproject_file_paths = []

for dirpath, dirnames, filenames in walk(getcwd()):
    for filename in filenames:
        if filename == "pyproject.toml":
            pyproject_file_paths.append(join(dirpath, filename))

for pyproject_file_path in pyproject_file_paths:
    if "_template" in pyproject_file_path:
        continue

    with open(pyproject_file_path) as pyproject_file:

        pyproject = parse(pyproject_file.read())

        if "hatch" not in pyproject["tool"].keys():
            continue

    version_file_path = pyproject["tool"]["hatch"]["version"]["path"]

    if version_file_path.endswith("version/__init__.py"):
        continue

    pyproject["tool"]["hatch"]["version"]["path"] = (
        pyproject["tool"]["hatch"]["version"]["path"]
        .replace(".py", "/__init__.py")
    )

    with open(pyproject_file_path, "w") as pyproject_file:
        pyproject_file.write(dumps(pyproject))

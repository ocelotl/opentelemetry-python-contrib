from os import walk, getcwd
from os.path import join


def find_pyproject_files(start_dir):
    pyproject_files = []
    for dirpath, dirnames, filenames in walk(start_dir):
        for filename in filenames:
            if filename == "pyproject.toml":
                full_path = join(dirpath, filename)
                pyproject_files.append(full_path)
    return pyproject_files


if __name__ == "__main__":
    current_dir = getcwd()  # Get the current directory
    pyproject_files = find_pyproject_files(current_dir)
    for file in pyproject_files:
        print(file)

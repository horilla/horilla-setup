from setuptools import setup, find_packages

setup(
    name="horillasetup",
    version="1.1.4",
    packages=find_packages(),
    # Deliberately empty. The migration executes inside the target project's
    # own interpreter, which already has Django and psycopg2; the tool must
    # stay installable globally, outside any project.
    install_requires=[],
    entry_points={
        "console_scripts": [
            "horillasetup=horillasetup.ctl:main",
        ],
    },
    author="Horilla",
    author_email="support@horilla.com",
    description=(
        "CLI tool to build, migrate and manage Horilla projects, including "
        "in-place upgrade of a Horilla v1 database to v2"
    ),
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/horilla/horilla-setup",
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Environment :: Console",
        "Intended Audience :: System Administrators",
        "Topic :: Database",
        "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.10",
)

# coding=utf-8
from setuptools import find_packages, setup

plugin_identifier = "octocam"
plugin_package = "octoprint_octocam"
plugin_name = "OctoPrint-Octocam"
plugin_version = "0.1.0"
plugin_description = (
    "Low-latency WebRTC webcam streaming for OctoPrint powered by go2rtc, "
    "with dynamic Cloudflare Calls TURN credentials and multi-stream selection."
)
plugin_author = "Ryan"
plugin_url = "https://github.com/Ryan/Octocam"
plugin_license = "AGPLv3"

plugin_requires = [
    "requests>=2.28.0,<3.0.0",
]

plugin_additional_data = []
plugin_additional_packages = []
plugin_ignored_packages = []


def package_data_dirs(source, sub_folders):
    import os

    dirs = []
    for folder in sub_folders:
        for dirname, _, files in os.walk(os.path.join(source, folder)):
            dirname = os.path.relpath(dirname, source)
            for f in files:
                dirs.append(os.path.join(dirname, f))
    return dirs


setup(
    name=plugin_name,
    version=plugin_version,
    description=plugin_description,
    author=plugin_author,
    url=plugin_url,
    license=plugin_license,
    packages=find_packages(exclude=["tests", "tests.*"]),
    package_data={
        plugin_package: package_data_dirs(
            plugin_package, ["static", "templates"]
        )
    },
    include_package_data=True,
    zip_safe=False,
    install_requires=plugin_requires,
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-asyncio>=0.21.0",
        ]
    },
    entry_points={
        "octoprint.plugin": [
            f"{plugin_identifier} = {plugin_package}"
        ]
    },
    python_requires=">=3.9,<4",
)

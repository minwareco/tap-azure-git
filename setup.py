#!/usr/bin/env python

import os

from setuptools import setup, find_packages

UTILS_VERSION = "21a1d4b552e2a821f9414bd9a35377e5892a260b"

setup(name='tap-azure-git',
      version='0.1',
      description='Singer tap for Azure DevOps API Git data',
      author='minWare',
      classifiers=['Programming Language :: Python :: 3 :: Only'],
      py_modules=['tap_azure_git'],
      install_requires=[
          'singer-python==6.1.0',
          'requests==2.20.0',
          'psutil==5.8.0',
          'minware_singer_utils@git+https://{}github.com/minwareco/minware-singer-utils.git@{}'.format(
              "{}@".format(os.environ.get("GITHUB_TOKEN")) if os.environ.get("GITHUB_TOKEN") else "",
              UTILS_VERSION
          )
      ],
      extras_require={
          'dev': [
              'pylint',
              'ipdb',
              'nose',
          ]
      },
      entry_points='''
          [console_scripts]
          tap-azure-git=tap_azure_git:main
      ''',
      packages=['tap_azure_git'],
      package_data = {
          'tap_azure_git': ['tap_azure_git/schemas/*.json']
      },
      include_package_data=True
)

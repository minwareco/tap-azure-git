#!/usr/bin/env python

import os

from setuptools import setup, find_packages

UTILS_VERSION = "22f493552c4eb46b2b5a6d98d7acacd9fb7edf68"

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
          'minware-singer-utils@git+https://{}@github.com/minwareco/minware-singer-utils.git{}'.format(
              os.environ.get("GITHUB_TOKEN", ""),
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

#!/usr/bin/env python

from setuptools import setup, find_packages

setup(name='tap-azure-git',
      version='0.1',
      description='Singer tap for Azure DevOps API Git data',
      author='minWare',
      classifiers=['Programming Language :: Python :: 3 :: Only'],
      py_modules=['tap_azure_git'],
      install_requires=[
          'singer-python==5.12.1',
          'requests==2.20.0',
          'psutil==5.8.0',
          'gitlocal@git+https://{}@github.com/minwareco/gitlocal.git'.format(os.environ.get("GITHUB_TOKEN", ""))
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

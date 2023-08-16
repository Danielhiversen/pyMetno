from setuptools import setup

setup(
    name = 'PyMetno',
    packages = ['metno'],
    install_requires=['xmltodict', "aiohttp>=3.0.6", "async_timeout>=3.0.0", 'pytz'],
    version = '0.11.0',
    description = 'A library to communicate with the met.no api',
    author='Daniel Hjelseth Høyer',
    url='https://github.com/Danielhiversen/pyMetno/',
    license='MIT',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Environment :: Other Environment',
        'Intended Audience :: Developers',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Topic :: Home Automation',
        'Topic :: Software Development :: Libraries :: Python Modules'
        ]
)

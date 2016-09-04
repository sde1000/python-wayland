from setuptools import setup

def readme():
    with open('README.md') as f:
        return f.read()

setup(name='wayland',
      version='0.1',
      description='Wayland protocol implementation',
      long_description=readme(),
      classifiers=[
          'Development Status :: 3 - Alpha',
          'License :: OSI Approved :: MIT License',
          'Programming Language :: Python :: 3.5',
          'Topic :: Software Development :: Libraries',
          'Intended Audience :: Developers',
      ],
      url='https://github.com/sde1000/python-wayland',
      author='Stephen Early',
      author_email='steve@assorted.org.uk',
      license='MIT',
      packages=['wayland'],
      zip_safe=True,
      test_suite='tests.test_wayland',
)

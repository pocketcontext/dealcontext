#!/usr/bin/python3 -I
"""Retired legacy deployment command; never modify a deployment host."""
import sys


def main():
    print('This legacy DealContext deployment command is retired. '
          'Use the maintained once-pocketcontext-v2 shared dispatcher and its '
          'app-specific restricted SSH key; see docs/ci-and-deployment.md. '
          'No deployment or installation was performed.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())

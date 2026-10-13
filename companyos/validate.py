"""`python -m companyos.validate`: validate the QUMATIC pack (this checkout) with the kit's validator."""
import sys

from orchestrator_kit.validate import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

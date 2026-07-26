"""
AID PLUS+ — Package Root
==========================
    from aidplus import main_menu
    main_menu()

Or run directly:
    python -m aidplus
"""
__version__ = "8.0.0"
__build__   = 29
__product__ = "AID PLUS+"
__platform__ = "Adwene ADW-1"

def main_menu():
    from aidplus.main import main_menu as _main
    return _main()

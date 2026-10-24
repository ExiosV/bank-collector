"""bank-collector — read-only collector of *your own* bank cabinet data.

Hard rules of this package (see docs/SECURITY.md):

* it NEVER logs in — it attaches over CDP to a browser you already opened and
  logged into by hand;
* it NEVER asks for, stores or transmits passwords, SMS codes or cookies;
* it is read-only for everything except the opt-in static-code screen: it only
  reads the already rendered DOM and never clicks
  "download statement", "pay", "transfer" or any other mutating control;
* everything stays on your disk — the default output directory is ``data/``
  which is git-ignored.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]

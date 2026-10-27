"""bank-collector — read-only collector of *your own* bank cabinet data.

Hard rules of this package (see docs/SECURITY.md):

* it NEVER logs in — it attaches over CDP to a browser you already opened and
  logged into by hand;
* it NEVER asks for, stores or transmits passwords, SMS codes or cookies;
* it is read-only for everything except three narrow, opt-in cases: the
  static-code screen (``--enter-code``), the list's own "load more" button
  when the profile names its exact text and exactly one such button exists,
  and the history's own "next page" arrow (``pages.next_selector``) of a list
  cut into numbered pages, pressed only when it resolves to exactly one
  element. Otherwise it only reads the already rendered DOM (scrolling it is
  read-only too — it moves the viewport) and never clicks "download
  statement", "pay", "transfer" or any other mutating control;
* everything stays on your disk — the default output directory is ``data/``
  which is git-ignored.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]

"""Checked-in test fixtures.

``tests/fixtures/p3/`` holds two different kinds of thing, and the distinction matters:

* **Captured transcripts** — real output from the real vendor runtimes on this host, scrubbed of
  the home directory path and of session UUIDs. They are the corpus the pure decoders in
  ``tce_supervisor.adapters.*`` are tested against, which is what makes those decoders testable
  without a vendor binary, a credential or a network.
* **Fake executors** — small Python programs that speak the same protocols on stdout. They give the
  adapter tests a real process lifecycle (spawn, stdout pump, exit code, SIGKILL, process group)
  with no network, no API key and no vendor binary anywhere in the picture.
"""

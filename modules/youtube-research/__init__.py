"""YouTube Research - an AgeniusDesk community module.

Paste a YouTube link in the Research tab -> fetch its caption track
(captions-only, no GPU, no sidecar) -> LLM breakdown of the concepts and how to
apply them -> the breakdown is filed into the notes vault under research/<topic>/,
classified into one of the existing topic folders. See router.py.
"""

from .router import router  # noqa: F401

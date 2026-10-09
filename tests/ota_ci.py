"""CI fixture: real Docker replacement using a locally built immutable image ID.
Registry metadata and pinned download behavior are covered separately by unit tests.
This file is mounted only by the test workflow, never in production images.
"""
import updater as u

def target():
    d=u.client()
    try:return d.images.get('islevetah-app:ci-target').id
    finally:d.close()

u.remote_digest=target
u.pull_image=lambda d,digest:d.images.get(digest)
u.restore_state()
u.ThreadingHTTPServer(('0.0.0.0',7790),u.Handler).serve_forever()

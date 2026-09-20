# TODO

## Push alerts for abnormal usage

`/stats` and `/api/hourly-stats` flag clients whose current hour is well
above their own baseline, but only on screen / via the JSON API. Nothing is
pushed. Next step is a notification (email, using the Claude Mail SMTP setup
in this workspace, or Slack) once the thresholds in `[analytics]` have been
tuned against real traffic, so it doesn't just generate noise.

Still open: which metric should page someone. The page currently flags on
bytes, requests or new downloads; a re-download of the same cached
installer looks very different from a client pulling many distinct large
files.

## Multi-machine scaling

The worker pool scales across the cores of one box. Several machines would
each need their own cache and SQLite index today; sharing them would mean
moving the index to a real database and the cache to shared storage.

## Scale-down can be slow under steady traffic

A worker is only retired when it holds no client connections (stopping it
would drop them). If every worker always has at least one open connection,
the pool won't shrink back to `min_workers`. A proper fix needs a way to
stop new connections reaching one worker while it drains, which
`SO_REUSEPORT` doesn't offer on its own.

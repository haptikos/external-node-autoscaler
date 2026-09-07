# fake

Not a cloud. A provider-shaped test double that keeps machines in a dict, so the
whole reconcile loop — rent, boot, peer, idle, reap — runs in a unit test with
no network, no credentials and no bill.

It is a real provider, not a mock: it implements the same `Provider` contract as
`ovh` and `vultr`, and the tests assert on the state it holds rather than on
"was called". That is deliberate. The two most expensive bugs in this project
were arithmetic in `controller.py`, and neither was reproducible without renting
a GPU; a stub that only recorded calls would have caught neither.

## Using it

Nothing to configure. It is selected like any other provider — a `providers`
entry naming `fake` — and each pool's placement carries the flavors and
capacity the test wants:

```yaml
providers:
  - name: fake
    credentialsSecret: unused
```

`FakeProvider(...)` takes the knobs the tests actually need: `capacity` per
placement, `assign_ip` to model a machine with no address yet, `create_raises`
and `list_raises` to model a cloud having a bad day, and `hidden` to model an
instance the inventory forgets for a pass.

## Things worth knowing

**It is shipped, not test-only.** It lives beside the real providers and is
loaded by the same `load_all`, so `providers.available()` lists it. That is what
makes it useful for a dry run against a real cluster — pools scale, peering is
attempted, nothing is bought — but it also means it must never be named in a
production values file by accident. A pool renting from `fake` reports capacity
that does not exist, and its pods will stay Pending against a virtual node that
never appears.

**`batch_size` defaults to 3 here, not `DEFAULT_BATCH_SIZE`.** The real
providers share one constant; this one keeps a small number so a test that
forgets to set it cannot rent fifty imaginary machines and bury the assertion.

**It does not simulate time.** Machines become "ready" when the test says so,
and ages come from `FakeClock`, which the tests advance by hand. Nothing here
sleeps, so a 20-minute boot timeout costs no wall clock.

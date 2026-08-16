"""Host adapters (A3): per-host hook sources plus install support.

``hosts.opencode`` ships the OpenCode plugin (plugin.ts, wheel package data);
``hosts.install`` writes/removes it under the host's global config root and
reports install state + daemon reachability. Host hooks are the capture main
channel (design/01 §4.5, decision 5): message events -> /ingest, session
lifecycle -> /session/end, pre-compact rescue -> /flush.
"""

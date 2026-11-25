"""Long-term incident memory and the Firestore checkpointer. Owner: Sai. See docs/INTERFACES.md §3.

store.py: the IncidentMemory interface and a local in-memory implementation. The Firestore store
and the Firestore checkpointer come later (GCP is on hold).

Shared shapes come from `contracts`; never redefine them here.
"""

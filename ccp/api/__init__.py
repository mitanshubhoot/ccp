"""The HTTP API (spec.md A-134): FastAPI over the same services the command line uses.

Every request is signed in (a bearer token verified on the server, A-129); the tenant comes
from the path, and the caller must be a member of it, with the role each action needs
(A-130). Nothing here holds a rule of its own: the approval store, the executor, the
exception and tenant services decide, and this layer maps their answers onto HTTP.
"""

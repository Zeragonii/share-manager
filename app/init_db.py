from .db import Base, engine
from . import models  # noqa: F401

Base.metadata.create_all(bind=engine)
print("Database schema ready")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.models import PaymentSource


def test_payment_source_can_be_archived_without_deletion():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    source = PaymentSource(name="Revolut", active=True)
    db.add(source); db.commit()
    source.active = False
    db.commit(); db.refresh(source)
    assert source.name == "Revolut"
    assert source.active is False

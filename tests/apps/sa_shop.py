"""A small SQLAlchemy data layer shared by the script, Flask and FastAPI tests."""

from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

metadata = MetaData()
customers = Table("customers", metadata, Column("id", Integer, primary_key=True), Column("name", String(50)),
                  Column("email", String(100)))
orders = Table("orders", metadata, Column("id", Integer, primary_key=True),
               Column("customer_id", Integer, ForeignKey("customers.id")), Column("total", Integer))


def make_engine():
    return create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})


def seed(conn):
    metadata.create_all(conn)
    for i in range(6):
        conn.execute(customers.insert().values(id=i + 1, name="Customer %d" % i, email="c%d@secret.example" % i))
        conn.execute(orders.insert().values(id=i + 1, customer_id=i + 1, total=10 * i))


def customer_names(engine):
    with engine.connect() as conn:
        rows = conn.execute(select(orders).order_by(orders.c.id)).fetchall()  # origin: sa list
        return [
            conn.execute(select(customers.c.name).where(customers.c.id == row.customer_id)).scalar()  # origin: sa n+1
            for row in rows
        ]


def order_total(engine, order_id):
    with Session(engine) as session:
        return session.execute(text("SELECT total FROM orders WHERE id = :id"), {"id": order_id}).scalar()  # origin: sa orm


def find_by_email(engine, email):
    with engine.connect() as conn:
        return conn.execute(select(customers.c.name).where(customers.c.email == email)).scalar()  # origin: sa email


def broken(engine):
    with engine.connect() as conn:
        conn.execute(text("SELECT nope FROM missing_table"))  # origin: sa broken


async def async_customer_names(async_engine):
    async with async_engine.connect() as conn:
        rows = (await conn.execute(select(orders).order_by(orders.c.id))).fetchall()  # origin: sa async list
        names = []
        for row in rows:
            result = await conn.execute(select(customers.c.name).where(customers.c.id == row.customer_id))  # origin: sa async n+1
            names.append(result.scalar())
        return names

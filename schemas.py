from sqlalchemy import Column, Integer, String, Float, Date, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from .database import Base

class Hotel(Base):
    __tablename__ = "hoteles"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    
    movements = relationship("Movement", back_populates="hotel")

class User(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String) # 'admin' or 'operator'

class Provider(Base):
    __tablename__ = "proveedores"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    rut = Column(String, unique=True, index=True)
    
    movements = relationship("Movement", back_populates="provider")

class Movement(Base):
    __tablename__ = "movimientos"
    id = Column(Integer, primary_key=True, index=True)
    hotel_id = Column(Integer, ForeignKey("hoteles.id"))
    date = Column(Date)
    description = Column(String)
    account_name = Column(String)
    provider_id = Column(Integer, ForeignKey("proveedores.id"))
    doc_type = Column(String)
    doc_number = Column(String)
    total = Column(Float)
    iva = Column(Float)
    net = Column(Float)
    observations = Column(String, nullable=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"))

    hotel = relationship("Hotel", back_populates="movements")
    provider = relationship("Provider", back_populates="movements")
    user = relationship("User")

    # Validación de duplicados: proveedor + tipo doc + núm doc
    __table_args__ = (UniqueConstraint('provider_id', 'doc_type', 'doc_number', name='_provider_doc_uc'),)

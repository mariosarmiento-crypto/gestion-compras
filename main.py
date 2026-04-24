import os
from datetime import datetime, timedelta, date as date_type
from io import BytesIO
from typing import Optional, List

import bcrypt
import jwt
import pandas as pd
from fastapi import FastAPI, Depends, HTTPException, status, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import Column, Date, Float, ForeignKey, Integer, String, UniqueConstraint, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, relationship, sessionmaker

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

# =========================
# CONFIGURACIÓN GENERAL
# =========================
SECRET_KEY = os.getenv("SECRET_KEY", "CAMBIAR_ESTA_CLAVE_SECRETA")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./gastos.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# =========================
# BASE DE DATOS
# =========================
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
    role = Column(String, default="operator")

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
    __table_args__ = (UniqueConstraint("provider_id", "doc_type", "doc_number", name="_provider_doc_uc"),)

# =========================
# SCHEMAS
# =========================
class HotelOut(BaseModel):
    id: int
    name: str
    class Config:
        from_attributes = True

class ProviderOut(BaseModel):
    id: int
    name: str
    rut: str
    class Config:
        from_attributes = True

class UserOut(BaseModel):
    id: int
    username: str
    role: str
    class Config:
        from_attributes = True

class MovementCreate(BaseModel):
    hotel_id: int
    date: date_type
    description: str
    account_name: str
    provider_name: str
    provider_rut: str
    doc_type: str
    doc_number: str
    total: float
    iva: float
    net: float
    observations: Optional[str] = None

class MovementOut(BaseModel):
    id: int
    hotel_id: int
    date: date_type
    description: str
    account_name: str
    provider_id: int
    doc_type: str
    doc_number: str
    total: float
    iva: float
    net: float
    observations: Optional[str] = None
    hotel: HotelOut
    provider: ProviderOut
    user: Optional[UserOut] = None
    class Config:
        from_attributes = True

class Token(BaseModel):
    access_token: str
    token_type: str

# =========================
# UTILIDADES
# =========================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Token inválido")
    except Exception:
        raise HTTPException(status_code=401, detail="No autorizado")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")
    return user

def generate_excel(rows):
    output = BytesIO()
    pd.DataFrame(rows).to_excel(output, index=False, sheet_name="Registros", engine="openpyxl")
    output.seek(0)
    return output

def generate_pdf(rows, title="Registro de compras y gastos"):
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=landscape(letter))
    styles = getSampleStyleSheet()
    elements = [Paragraph(title, styles["Title"])]
    if not rows:
        elements.append(Paragraph("No hay datos para mostrar.", styles["Normal"]))
    else:
        headers = list(rows[0].keys())
        data = [headers] + [[str(item.get(h, "")) for h in headers] for item in rows]
        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
        ]))
        elements.append(table)
    doc.build(elements)
    output.seek(0)
    return output

# =========================
# APP
# =========================
app = FastAPI(title="Gestión de Compras y Gastos")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
Base.metadata.create_all(bind=engine)

@app.on_event("startup")
def startup_populate():
    db = SessionLocal()
    try:
        for hotel_name in ["Mare Hotel", "Reñaca House"]:
            if not db.query(Hotel).filter(Hotel.name == hotel_name).first():
                db.add(Hotel(name=hotel_name))
        if not db.query(User).filter(User.username == "admin").first():
            db.add(User(username="admin", hashed_password=get_password_hash("admin123"), role="admin"))
        db.commit()
    finally:
        db.close()

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML_PAGE)

@app.post("/token", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario o clave incorrecta")
    return {"access_token": create_access_token({"sub": user.username}), "token_type": "bearer"}

@app.get("/hoteles", response_model=List[HotelOut])
def hoteles(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return db.query(Hotel).all()

@app.get("/proveedores", response_model=List[ProviderOut])
def proveedores(q: Optional[str] = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    query = db.query(Provider)
    if q:
        query = query.filter(Provider.name.contains(q))
    return query.order_by(Provider.name).all()

@app.get("/cuentas")
def cuentas(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(Movement.account_name).distinct().all()
    return [r[0] for r in rows if r[0]]

@app.post("/movimientos", response_model=MovementOut)
def crear_movimiento(movement: MovementCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    provider = db.query(Provider).filter(Provider.name == movement.provider_name).first()
    if provider and provider.rut != movement.provider_rut:
        raise HTTPException(status_code=400, detail=f"El proveedor ya existe con otro RUT: {provider.rut}")
    if not provider:
        provider_by_rut = db.query(Provider).filter(Provider.rut == movement.provider_rut).first()
        if provider_by_rut:
            raise HTTPException(status_code=400, detail=f"El RUT ya está asociado al proveedor {provider_by_rut.name}")
        provider = Provider(name=movement.provider_name, rut=movement.provider_rut)
        db.add(provider)
        db.commit()
        db.refresh(provider)
    db_movement = Movement(
        hotel_id=movement.hotel_id,
        date=movement.date,
        description=movement.description,
        account_name=movement.account_name,
        provider_id=provider.id,
        doc_type=movement.doc_type,
        doc_number=movement.doc_number,
        total=movement.total,
        iva=movement.iva,
        net=movement.net,
        observations=movement.observations,
        user_id=user.id,
    )
    try:
        db.add(db_movement)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=400, detail="Registro duplicado: proveedor + tipo de documento + número de documento")
    db.refresh(db_movement)
    return db_movement

@app.get("/movimientos", response_model=List[MovementOut])
def listar_movimientos(
    hotel_id: Optional[int] = None,
    date_from: Optional[date_type] = None,
    date_to: Optional[date_type] = None,
    provider_id: Optional[int] = None,
    doc_number: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Movement)
    if hotel_id:
        query = query.filter(Movement.hotel_id == hotel_id)
    if date_from:
        query = query.filter(Movement.date >= date_from)
    if date_to:
        query = query.filter(Movement.date <= date_to)
    if provider_id:
        query = query.filter(Movement.provider_id == provider_id)
    if doc_number:
        query = query.filter(Movement.doc_number.contains(doc_number))
    return query.order_by(Movement.date.desc(), Movement.id.desc()).all()

@app.delete("/movimientos/{movement_id}")
def eliminar_movimiento(movement_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    mov = db.query(Movement).filter(Movement.id == movement_id).first()
    if not mov:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    db.delete(mov)
    db.commit()
    return {"message": "Registro eliminado"}

def export_rows(db: Session, hotel_id=None, date_from=None, date_to=None):
    query = db.query(Movement)
    if hotel_id:
        query = query.filter(Movement.hotel_id == hotel_id)
    if date_from:
        query = query.filter(Movement.date >= date_from)
    if date_to:
        query = query.filter(Movement.date <= date_to)
    rows = []
    for m in query.order_by(Movement.date.desc()).all():
        rows.append({
            "FECHA": m.date,
            "HOTEL": m.hotel.name,
            "DESCRIPCION": m.description,
            "NOMBRE DE CUENTA": m.account_name,
            "PROVEEDOR": m.provider.name,
            "RUT": m.provider.rut,
            "TIPO DE DOCUMENTO": m.doc_type,
            "NUMERO DOC.": m.doc_number,
            "MONTO TOTAL": m.total,
            "IVA": m.iva,
            "NETO": m.net,
            "OBSERVACIONES": m.observations or "",
        })
    return rows

@app.get("/export/{format}")
def exportar(format: str, hotel_id: Optional[int] = None, date_from: Optional[date_type] = None, date_to: Optional[date_type] = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = export_rows(db, hotel_id, date_from, date_to)
    if format == "excel":
        file = generate_excel(rows)
        return Response(file.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=registros.xlsx"})
    if format == "pdf":
        file = generate_pdf(rows)
        return Response(file.getvalue(), media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=registros.pdf"})
    raise HTTPException(status_code=400, detail="Formato no soportado")

HTML_PAGE = r'''
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gestión de Compras y Gastos</title>
<style>
body{font-family:Arial,sans-serif;margin:0;background:#f5f6f8;color:#222}.wrap{max-width:1100px;margin:auto;padding:18px}.card{background:#fff;border-radius:14px;padding:18px;box-shadow:0 2px 12px #0001;margin-bottom:16px}input,select,textarea,button{font-size:15px;padding:10px;border:1px solid #ccc;border-radius:8px}button{background:#111;color:#fff;cursor:pointer;border:0}.secondary{background:#555}.danger{background:#b42318}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.hidden{display:none}table{width:100%;border-collapse:collapse;background:white}th,td{padding:8px;border-bottom:1px solid #ddd;text-align:left;font-size:13px}.top{display:flex;justify-content:space-between;gap:10px;align-items:center}.actions{display:flex;gap:8px;flex-wrap:wrap}.muted{color:#777;font-size:13px}@media(max-width:700px){.top{display:block}.actions{margin-top:10px}table{display:block;overflow:auto}.wrap{padding:10px}.card{padding:12px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card top">
    <div><h2>Gestión de Compras y Gastos</h2><div class="muted">Mare Hotel / Reñaca House</div></div>
    <div class="actions"><button onclick="showForm()">+ Nuevo registro</button><button class="secondary" onclick="logout()">Salir</button></div>
  </div>

  <div id="login" class="card">
    <h3>Ingreso</h3>
    <div class="grid"><input id="username" placeholder="Usuario" value="admin"><input id="password" type="password" placeholder="Clave" value="admin123"><button onclick="login()">Entrar</button></div>
  </div>

  <div id="app" class="hidden">
    <div id="formCard" class="card hidden">
      <h3>Nuevo registro</h3>
      <div class="grid">
        <select id="hotel_id"></select><input id="date" type="date"><input id="description" placeholder="Descripción / Glosa"><input id="account_name" list="accounts" placeholder="Nombre de cuenta"><datalist id="accounts"></datalist><input id="provider_name" list="providers" placeholder="Proveedor"><datalist id="providers"></datalist><input id="provider_rut" placeholder="RUT"><select id="doc_type"><option>Factura</option><option>Boleta</option><option>Factura exenta</option><option>Nota de crédito</option><option>Nota de débito</option><option>Otro</option></select><input id="doc_number" placeholder="Número doc."><input id="total" type="number" step="0.01" placeholder="Monto total"><input id="iva" type="number" step="0.01" placeholder="IVA"><input id="net" type="number" step="0.01" placeholder="Neto"><input id="observations" placeholder="Observaciones">
      </div><br><button onclick="saveMovement()">Guardar</button> <button class="secondary" onclick="hideForm()">Cancelar</button>
    </div>

    <div class="card">
      <h3>Filtros</h3>
      <div class="grid"><select id="filter_hotel"><option value="">Todos los hoteles</option></select><input id="date_from" type="date"><input id="date_to" type="date"><input id="doc_filter" placeholder="Nº documento"><button onclick="loadMovements()">Filtrar</button></div><br>
      <div class="actions"><button class="secondary" onclick="exportFile('excel')">Descargar Excel</button><button class="secondary" onclick="exportFile('pdf')">Descargar PDF</button></div>
    </div>

    <div class="card"><h3>Registros</h3><div style="overflow:auto"><table><thead><tr><th>Fecha</th><th>Hotel</th><th>Cuenta</th><th>Proveedor</th><th>RUT</th><th>Doc</th><th>Total</th><th>IVA</th><th>Neto</th><th></th></tr></thead><tbody id="rows"></tbody></table></div></div>
  </div>
</div>
<script>
let token=localStorage.getItem('token')||'';let providers=[];
function authHeaders(){return {'Authorization':'Bearer '+token,'Content-Type':'application/json'}}
async function login(){let body=new URLSearchParams();body.append('username',username.value);body.append('password',password.value);let r=await fetch('/token',{method:'POST',body});if(!r.ok){alert('Usuario o clave incorrecta');return}let j=await r.json();token=j.access_token;localStorage.setItem('token',token);init()}
function logout(){localStorage.removeItem('token');location.reload()}
function showForm(){formCard.classList.remove('hidden');date.value=new Date().toISOString().slice(0,10)}function hideForm(){formCard.classList.add('hidden')}
async function init(){if(!token)return;login.classList.add('hidden');app.classList.remove('hidden');await loadHotels();await loadProviders();await loadAccounts();await loadMovements()}
async function loadHotels(){let r=await fetch('/hoteles',{headers:authHeaders()});let data=await r.json();hotel_id.innerHTML='';filter_hotel.innerHTML='<option value="">Todos los hoteles</option>';data.forEach(h=>{hotel_id.innerHTML+=`<option value="${h.id}">${h.name}</option>`;filter_hotel.innerHTML+=`<option value="${h.id}">${h.name}</option>`})}
async function loadProviders(){let r=await fetch('/proveedores',{headers:authHeaders()});providers=await r.json();providers_list.innerHTML='';providers.forEach(p=>providers_list.innerHTML+=`<option value="${p.name}"></option>`)}
provider_name.addEventListener('change',()=>{let p=providers.find(x=>x.name===provider_name.value);if(p){provider_rut.value=p.rut;provider_rut.readOnly=true}else{provider_rut.value='';provider_rut.readOnly=false}})
async function loadAccounts(){let r=await fetch('/cuentas',{headers:authHeaders()});let data=await r.json();accounts.innerHTML='';data.forEach(c=>accounts.innerHTML+=`<option value="${c}"></option>`)}
async function saveMovement(){let data={hotel_id:+hotel_id.value,date:date.value,description:description.value,account_name:account_name.value,provider_name:provider_name.value,provider_rut:provider_rut.value,doc_type:doc_type.value,doc_number:doc_number.value,total:+total.value||0,iva:+iva.value||0,net:+net.value||0,observations:observations.value};let r=await fetch('/movimientos',{method:'POST',headers:authHeaders(),body:JSON.stringify(data)});if(!r.ok){alert((await r.json()).detail||'Error');return}document.querySelectorAll('#formCard input').forEach(i=>i.value='');hideForm();await loadProviders();await loadAccounts();await loadMovements()}
async function loadMovements(){let p=new URLSearchParams();if(filter_hotel.value)p.append('hotel_id',filter_hotel.value);if(date_from.value)p.append('date_from',date_from.value);if(date_to.value)p.append('date_to',date_to.value);if(doc_filter.value)p.append('doc_number',doc_filter.value);let r=await fetch('/movimientos?'+p.toString(),{headers:authHeaders()});if(!r.ok){logout();return}let data=await r.json();rows.innerHTML='';data.forEach(m=>rows.innerHTML+=`<tr><td>${m.date}</td><td>${m.hotel.name}</td><td>${m.account_name}</td><td>${m.provider.name}</td><td>${m.provider.rut}</td><td>${m.doc_type} ${m.doc_number}</td><td>${m.total}</td><td>${m.iva}</td><td>${m.net}</td><td><button class="danger" onclick="delMov(${m.id})">X</button></td></tr>`)}
async function delMov(id){if(!confirm('¿Eliminar registro?'))return;await fetch('/movimientos/'+id,{method:'DELETE',headers:authHeaders()});loadMovements()}
function exportFile(fmt){let p=new URLSearchParams();if(filter_hotel.value)p.append('hotel_id',filter_hotel.value);if(date_from.value)p.append('date_from',date_from.value);if(date_to.value)p.append('date_to',date_to.value);location.href='/export/'+fmt+'?'+p.toString()+'&token='+token}
init();
</script>
</body>
</html>
'''

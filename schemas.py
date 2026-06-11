import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator


def _validate_cpf_digits(digits: str) -> bool:
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    total = sum(int(digits[i]) * (10 - i) for i in range(9))
    r1 = (total * 10 % 11) % 10
    if r1 != int(digits[9]):
        return False
    total = sum(int(digits[i]) * (11 - i) for i in range(10))
    r2 = (total * 10 % 11) % 10
    return r2 == int(digits[10])


class CadastroCreate(BaseModel):
    nome_completo: str
    cpf: str
    telefone: str
    email: EmailStr
    hospital: str
    cargo: str
    tempo_servico: str
    filiado: bool
    zapsign_doc_token: str

    @field_validator("nome_completo")
    @classmethod
    def validate_nome(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Nome muito curto")
        if len(v) > 255:
            raise ValueError("Nome muito longo")
        return v

    @field_validator("cpf")
    @classmethod
    def validate_cpf(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if not _validate_cpf_digits(digits):
            raise ValueError("CPF inválido")
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"

    @field_validator("telefone")
    @classmethod
    def validate_telefone(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if len(digits) < 10 or len(digits) > 13:
            raise ValueError("Telefone inválido")
        return v.strip()

    @field_validator("hospital", "cargo", "tempo_servico")
    @classmethod
    def validate_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Campo obrigatório")
        if len(v) > 255:
            raise ValueError("Texto muito longo")
        return v

    @field_validator("zapsign_doc_token")
    @classmethod
    def validate_token(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Assinatura digital obrigatória")
        return v


class DocumentoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tipo: str
    nome_arquivo: str
    tamanho_bytes: Optional[int]
    created_at: datetime


class CadastroOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nome_completo: str
    cpf: str
    email: str
    hospital: str
    cargo: str
    status: str
    created_at: datetime


class CadastroAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nome_completo: str
    cpf: str
    telefone: str
    email: str
    hospital: str
    cargo: str
    tempo_servico: str
    filiado: bool
    status: str
    nota_interna: Optional[str]
    zapsign_doc_token: Optional[str]
    ip_cadastro: Optional[str]
    created_at: datetime
    updated_at: datetime
    documentos: list[DocumentoOut] = []


class AdminLoginIn(BaseModel):
    email: EmailStr
    senha: str


class StatusUpdateIn(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"novo", "em_andamento", "concluido"}
        if v not in allowed:
            raise ValueError(f"Status inválido. Permitidos: {allowed}")
        return v


class NotaUpdateIn(BaseModel):
    nota: str

    @field_validator("nota")
    @classmethod
    def validate_nota(cls, v: str) -> str:
        if len(v) > 5000:
            raise ValueError("Nota muito longa (máx 5000 chars)")
        return v


class ZapSignCreateIn(BaseModel):
    nome: str
    cpf: str

    @field_validator("nome")
    @classmethod
    def validate_nome(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Nome obrigatório")
        return v

    @field_validator("cpf")
    @classmethod
    def validate_cpf(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if not _validate_cpf_digits(digits):
            raise ValueError("CPF inválido")
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"


class ZapSignCreateOut(BaseModel):
    signer_token: str
    doc_token: str

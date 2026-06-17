import re
import uuid
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
    analise_estabilidade: bool = False
    rg: str
    data_nascimento: str
    estado_civil: str
    nacionalidade: str
    recebe_outro_beneficio: bool = False
    cep: str
    logradouro: str
    numero: str
    complemento: str = ""
    bairro: str
    cidade: str
    uf: str
    modalidade_atendimento: str

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

    @field_validator("rg")
    @classmethod
    def validate_rg(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 5 or len(v) > 20:
            raise ValueError("RG inválido")
        return v

    @field_validator("data_nascimento")
    @classmethod
    def validate_data_nascimento(cls, v: str) -> str:
        v = v.strip()
        try:
            dia = datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Data de nascimento inválida")
        hoje = datetime.now()
        idade = (hoje - dia).days / 365.25
        if idade < 14 or idade > 110:
            raise ValueError("Data de nascimento fora da faixa válida")
        return v

    @field_validator("estado_civil")
    @classmethod
    def validate_estado_civil(cls, v: str) -> str:
        v = v.strip()
        validos = {"Solteiro(a)", "Casado(a)", "Divorciado(a)", "Viúvo(a)", "União estável", "Separado(a)"}
        if v not in validos:
            raise ValueError("Estado civil inválido")
        return v

    @field_validator("nacionalidade")
    @classmethod
    def validate_nacionalidade(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3 or len(v) > 40:
            raise ValueError("Nacionalidade inválida")
        return v

    @field_validator("cep")
    @classmethod
    def validate_cep(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if len(digits) != 8:
            raise ValueError("CEP inválido")
        return f"{digits[:5]}-{digits[5:]}"

    @field_validator("logradouro", "numero", "bairro", "cidade")
    @classmethod
    def validate_endereco(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Endereço incompleto")
        if len(v) > 255:
            raise ValueError("Texto muito longo")
        return v

    @field_validator("complemento")
    @classmethod
    def validate_complemento(cls, v: str) -> str:
        return v.strip()[:100]

    @field_validator("uf")
    @classmethod
    def validate_uf(cls, v: str) -> str:
        v = v.strip().upper()
        ufs = {
            "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
            "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
            "SP", "SE", "TO",
        }
        if v not in ufs:
            raise ValueError("UF inválida")
        return v

    @field_validator("modalidade_atendimento")
    @classmethod
    def validate_modalidade(cls, v: str) -> str:
        if v not in ("online", "presencial"):
            raise ValueError("Modalidade inválida")
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


class LeadCreate(BaseModel):
    email: EmailStr
    consentimento_termos: bool
    consentimento_marketing: bool = False

    @field_validator("consentimento_termos")
    @classmethod
    def termos_obrigatorio(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Você precisa aceitar os termos para continuar")
        return v


class EventoSessaoCreate(BaseModel):
    session_id: str
    lead_id: Optional[str] = None
    tipo: str
    payload: Optional[dict] = None

    @field_validator("session_id")
    @classmethod
    def val_session(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except ValueError:
            raise ValueError("session_id inválido")
        return v

    @field_validator("tipo")
    @classmethod
    def val_tipo(cls, v: str) -> str:
        validos = {"secao_vista", "secao_concluida", "campo_blur", "upload_cnis", "abandono", "conversao"}
        if v not in validos:
            raise ValueError("tipo inválido")
        return v

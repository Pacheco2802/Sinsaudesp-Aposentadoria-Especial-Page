from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Cadastro(Base):
    __tablename__ = "cadastros"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nome_completo: Mapped[str] = mapped_column(String(255), nullable=False)
    cpf: Mapped[str] = mapped_column(String(14), nullable=False, unique=True, index=True)
    telefone: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    hospital: Mapped[str] = mapped_column(String(255), nullable=False)
    cargo: Mapped[str] = mapped_column(String(255), nullable=False)
    tempo_servico: Mapped[str] = mapped_column(String(100), nullable=False)
    filiado: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="novo", server_default="novo")
    nota_interna: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    zapsign_doc_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ip_cadastro: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now(), onupdate=func.now())

    documentos: Mapped[list["Documento"]] = relationship(
        "Documento",
        back_populates="cadastro",
        cascade="all, delete-orphan",
    )


class Documento(Base):
    __tablename__ = "documentos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cadastro_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cadastros.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tipo: Mapped[str] = mapped_column(String(50), nullable=False)
    nome_arquivo: Mapped[str] = mapped_column(String(255), nullable=False)
    caminho_arquivo: Mapped[str] = mapped_column(String(500), nullable=False)
    tamanho_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    cadastro: Mapped["Cadastro"] = relationship("Cadastro", back_populates="documentos")


class AdminUsuario(Base):
    __tablename__ = "admin_usuarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nome: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    senha_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

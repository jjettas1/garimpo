"""Linha de comando do garimpo.

Serve pra rodar o motor contra o banco antes de existir tela nenhuma:

    python -m app.cli usuario  --email eu@exemplo.com
    python -m app.cli sementes --email eu@exemplo.com "Duster" "Slint" "Codeine"
    python -m app.cli faixa    --email eu@exemplo.com --piso 500 --teto 80000
    python -m app.cli garimpar --email eu@exemplo.com --limite 10
    python -m app.cli listar   --email eu@exemplo.com
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import obter_config
from app.db import abrir_sessao
from app.garimpo import garimpar
from app.models import Discovery, SeedArtist, User

SENHA_PENDENTE = "!sem-senha-ainda"
"""Usuário criado pela CLI não tem senha — o hash de verdade vem na etapa 2."""


def _achar_usuario(sessao: Session, email: str) -> User:
    user = sessao.scalar(select(User).where(User.email == email))
    if user is None:
        sys.exit(f"usuário {email} não existe. crie com: python -m app.cli usuario --email {email}")
    return user


def cmd_usuario(args: argparse.Namespace) -> None:
    with abrir_sessao() as sessao:
        if sessao.scalar(select(User).where(User.email == args.email)):
            print(f"usuário {args.email} já existe")
            return
        config = obter_config()
        sessao.add(
            User(
                email=args.email,
                password_hash=SENHA_PENDENTE,
                email_verificado=True,
                piso_ouvintes=config.piso_ouvintes,
                teto_ouvintes=config.teto_ouvintes,
            )
        )
        print(f"usuário {args.email} criado (sem senha — login só na etapa 2)")


def cmd_sementes(args: argparse.Namespace) -> None:
    with abrir_sessao() as sessao:
        user = _achar_usuario(sessao, args.email)

        if args.nomes:
            for semente in list(user.sementes):
                sessao.delete(semente)
            sessao.flush()
            for ordem, nome in enumerate(args.nomes):
                sessao.add(SeedArtist(user_id=user.id, nome=nome, ordem=ordem))
            sessao.flush()
            sessao.refresh(user)

        if not user.sementes:
            print("nenhuma semente cadastrada")
            return
        print(f"sementes de {user.email}:")
        for semente in user.sementes:
            print(f"  {semente.ordem + 1}. {semente.nome}")
        if not 3 <= len(user.sementes) <= 8:
            print(f"  aviso: o produto pede de 3 a 8 sementes, você tem {len(user.sementes)}")


def cmd_faixa(args: argparse.Namespace) -> None:
    with abrir_sessao() as sessao:
        user = _achar_usuario(sessao, args.email)
        if args.piso is not None:
            user.piso_ouvintes = args.piso
        if args.teto is not None:
            user.teto_ouvintes = args.teto
        if user.piso_ouvintes >= user.teto_ouvintes:
            sys.exit("o piso de ouvintes precisa ser menor que o teto")
        print(f"faixa de ouvintes: {user.piso_ouvintes:,} a {user.teto_ouvintes:,}")


def _mostrar(descoberta: Discovery, indice: int) -> None:
    artista = descoberta.artista
    print(f"\n[{indice:02d}] {artista.nome}  ·  {artista.listeners or 0:,} ouvintes")
    print(
        f"     score {descoberta.score:.4f}  "
        f"(afinidade {descoberta.afinidade:.3f} · "
        f"raridade {descoberta.raridade:.3f} · "
        f"coerência {descoberta.coerencia:.3f} · "
        f"{descoberta.n_rotas} rota(s))"
    )
    melhor = descoberta.rota[0]
    caminho = " -> ".join(
        [melhor["passos"][0]["de"]] + [passo["para"] for passo in melhor["passos"]]
    )
    print(f"     rota: {caminho}")
    if artista.tags:
        print(f"     tags: {', '.join(artista.tags[:5])}")


def cmd_garimpar(args: argparse.Namespace) -> None:
    with abrir_sessao() as sessao:
        user = _achar_usuario(sessao, args.email)
        if not user.sementes:
            sys.exit("cadastre as sementes antes: python -m app.cli sementes --email ...")

        print(f"garimpando a partir de {len(user.sementes)} semente(s)... (pode demorar)")
        descobertas = garimpar(sessao, user, limite=args.limite)

        if not descobertas:
            print("nada dentro da faixa de ouvintes. tente afrouxar o piso/teto.")
            return
        for indice, descoberta in enumerate(descobertas, start=1):
            _mostrar(descoberta, indice)
        print(f"\n{len(descobertas)} descoberta(s) gravada(s).")


def cmd_listar(args: argparse.Namespace) -> None:
    with abrir_sessao() as sessao:
        user = _achar_usuario(sessao, args.email)
        descobertas = sessao.scalars(
            select(Discovery)
            .where(Discovery.user_id == user.id)
            .order_by(Discovery.score.desc())
            .limit(args.limite)
        ).all()
        if not descobertas:
            print("nenhuma descoberta ainda")
            return
        for indice, descoberta in enumerate(descobertas, start=1):
            _mostrar(descoberta, indice)


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="garimpo", description="motor de descoberta, sem tela")
    sub = parser.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("usuario", help="cria um usuário local")
    p.add_argument("--email", required=True)
    p.set_defaults(func=cmd_usuario)

    p = sub.add_parser("sementes", help="define ou mostra as sementes")
    p.add_argument("--email", required=True)
    p.add_argument("nomes", nargs="*", help="de 3 a 8 artistas; sem nomes, só mostra")
    p.set_defaults(func=cmd_sementes)

    p = sub.add_parser("faixa", help="ajusta piso e teto de ouvintes")
    p.add_argument("--email", required=True)
    p.add_argument("--piso", type=int)
    p.add_argument("--teto", type=int)
    p.set_defaults(func=cmd_faixa)

    p = sub.add_parser("garimpar", help="roda uma rodada de descoberta")
    p.add_argument("--email", required=True)
    p.add_argument("--limite", type=int, default=20)
    p.set_defaults(func=cmd_garimpar)

    p = sub.add_parser("listar", help="mostra o que já foi descoberto")
    p.add_argument("--email", required=True)
    p.add_argument("--limite", type=int, default=20)
    p.set_defaults(func=cmd_listar)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = construir_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

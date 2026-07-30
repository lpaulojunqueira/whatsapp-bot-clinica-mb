"""
Importação de agenda (pacientes já agendados) por planilha CSV ou Excel.

Serve pra clínica passar a agenda que já tem (Google Agenda exportado, Excel,
sistema antigo) pro Converte.ai, de modo que a Ana possa mandar a confirmação
no dia da consulta. O que importa é ter telefone + data + hora de cada paciente.

Formato esperado (linha de cabeçalho, nomes flexíveis):
  nome | telefone | data | hora | observação(opcional)

Este módulo só faz PARSE e NORMALIZAÇÃO — não toca no banco. Nunca levanta
exceção pra fora de `parse_planilha`: linhas problemáticas viram itens em `erros`.
"""
import csv
import io
import re
from datetime import datetime, date as date_cls, time as time_cls


# Sinônimos aceitos por coluna (comparados sem acento, minúsculos)
_COLS = {
    "nome": ["nome", "paciente", "cliente", "nome do paciente", "nome completo"],
    "telefone": ["telefone", "celular", "whatsapp", "fone", "contato", "tel", "numero", "número"],
    "data": ["data", "dia", "data da consulta"],
    "hora": ["hora", "horario", "horário", "hora da consulta"],
    "observacao": ["observacao", "observação", "obs", "motivo", "procedimento", "tratamento", "descricao", "descrição"],
}


def _sem_acento(s):
    tab = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüç", "aaaaaeeeeiiiiooooouuuuc")
    return (s or "").strip().lower().translate(tab)


def _mapear_colunas(header):
    """Recebe a linha de cabeçalho (lista) e devolve {campo: índice}."""
    idx = {}
    norm = [_sem_acento(str(h)) for h in header]
    for campo, sinonimos in _COLS.items():
        for i, h in enumerate(norm):
            if h in sinonimos:
                idx[campo] = i
                break
    return idx


def normalizar_telefone(bruto):
    """
    Devolve o número no formato que o WhatsApp aceita (55 + DDD + número) ou
    None se claramente inválido. Aceita '(19) 99999-9999', '19999999999',
    '5519999999999', etc.
    """
    if bruto is None:
        return None
    # openpyxl pode entregar número (float/int) — tira o .0
    if isinstance(bruto, float):
        bruto = str(int(bruto))
    d = re.sub(r"\D", "", str(bruto))
    d = d.lstrip("0")
    if not d:
        return None
    if d.startswith("55") and len(d) in (12, 13):
        return d
    if len(d) in (10, 11):          # DDD + número, sem DDI
        return "55" + d
    if len(d) in (12, 13):          # já tem 12-13 dígitos mas não começa com 55
        return d
    return None                     # curto/estranho demais


def _parse_data(v):
    """-> 'YYYY-MM-DD' ou None."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date_cls):
        return v.isoformat()
    s = str(v).strip()
    # pega só a parte de data se vier 'DD/MM/YYYY HH:MM'
    s = s.split(" ")[0]
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_hora(v):
    """-> 'HH:MM' ou None."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.strftime("%H:%M")
    if isinstance(v, time_cls):
        return v.strftime("%H:%M")
    s = str(v).strip()
    # 'DD/MM/YYYY HH:MM' -> pega a hora
    if " " in s and ("/" in s or "-" in s):
        s = s.split(" ")[-1]
    s = s.replace("h", ":").replace("H", ":").strip()
    s = s.rstrip(":")           # '9h' -> '9:' -> '9'  (hora cheia)
    m = re.match(r"^(\d{1,2}):(\d{2})", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    m = re.match(r"^(\d{1,2})$", s)   # só a hora cheia
    if m:
        hh = int(m.group(1))
        if 0 <= hh <= 23:
            return f"{hh:02d}:00"
    return None


def _linhas_do_arquivo(nome_arquivo, conteudo_bytes):
    """Devolve (header, linhas) a partir do CSV/XLSX. Levanta ValueError se não der."""
    ext = (nome_arquivo or "").lower().rsplit(".", 1)[-1]
    if ext in ("xlsx", "xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise ValueError("suporte a Excel indisponível no servidor")
        wb = openpyxl.load_workbook(io.BytesIO(conteudo_bytes), read_only=True, data_only=True)
        ws = wb.active
        linhas = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    else:
        # CSV — tenta utf-8 (com BOM) e cai pra latin-1; detecta ; ou ,
        try:
            texto = conteudo_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            texto = conteudo_bytes.decode("latin-1", errors="replace")
        amostra = texto[:2048]
        delim = ";" if amostra.count(";") > amostra.count(",") else ","
        linhas = [row for row in csv.reader(io.StringIO(texto), delimiter=delim)]
    # remove linhas totalmente vazias
    linhas = [l for l in linhas if l and any((c is not None and str(c).strip()) for c in l)]
    if not linhas:
        raise ValueError("arquivo vazio")
    return linhas[0], linhas[1:]


def parse_planilha(nome_arquivo, conteudo_bytes):
    """
    Lê o arquivo e devolve (validos, erros).
      validos: [{nome, telefone, data:'YYYY-MM-DD', hora:'HH:MM', observacao}]
      erros:   [{linha:int, motivo:str}]
    Nunca levanta — problema de leitura vira erro geral em `erros`.
    """
    try:
        header, linhas = _linhas_do_arquivo(nome_arquivo, conteudo_bytes)
    except ValueError as e:
        return [], [{"linha": 0, "motivo": str(e)}]
    except Exception as e:
        return [], [{"linha": 0, "motivo": f"não consegui ler o arquivo ({e})"}]

    idx = _mapear_colunas(header)
    faltando = [c for c in ("telefone", "data", "hora") if c not in idx]
    if faltando:
        return [], [{"linha": 1, "motivo": "faltam colunas obrigatórias: " + ", ".join(faltando)
                     + ". Cabeçalho esperado: nome, telefone, data, hora, observação."}]

    def val(row, campo):
        i = idx.get(campo)
        if i is None or i >= len(row):
            return None
        return row[i]

    validos, erros = [], []
    for n, row in enumerate(linhas, start=2):   # linha 1 = cabeçalho
        tel = normalizar_telefone(val(row, "telefone"))
        data = _parse_data(val(row, "data"))
        hora = _parse_hora(val(row, "hora"))
        nome = (str(val(row, "nome")).strip() if val(row, "nome") is not None else "")
        obs = (str(val(row, "observacao")).strip() if val(row, "observacao") is not None else "")
        if not tel:
            erros.append({"linha": n, "motivo": "telefone inválido ou vazio"})
            continue
        if not data:
            erros.append({"linha": n, "motivo": "data inválida (use DD/MM/AAAA)"})
            continue
        if not hora:
            erros.append({"linha": n, "motivo": "hora inválida (use HH:MM)"})
            continue
        validos.append({
            "nome": nome, "telefone": tel, "data": data, "hora": hora,
            "observacao": obs,
        })
    return validos, erros

"""
Servidor da Ana — atendimento por IA no WhatsApp.

Recebe os webhooks do WhatsApp Cloud API, identifica de QUAL clínica é,
processa a mensagem em segundo plano (com delay natural e indicador de
digitação), e responde via Claude.
"""

from flask import Flask, request, jsonify
import os
import requests
import threading
import time
import random
import re
from datetime import datetime, timedelta, timezone

from db import (
    inicializar_banco,
    buscar_clinica_por_phone_id,
    obter_ou_criar_conversa,
    mensagem_ja_processada,
    salvar_mensagem,
    obter_historico_conversa,
    conversa_esta_pausada,
    obter_horarios_disponiveis,
    obter_horarios_disponiveis_intervalo,
    criar_agendamento,
    cancelar_agendamento,
    obter_agendamento,
    obter_config_horarios,
)
from painel import registrar_rotas as registrar_rotas_painel

app = Flask(__name__)

# Chave de assinatura dos cookies de sessão (login do painel).
# Se SECRET_KEY não estiver definida, usa um valor padrão (NÃO seguro pra produção).
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "troque-essa-chave-em-producao")

# ============================================================
# CONFIGURAÇÕES
# ============================================================
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "meu_token_secreto_123")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
WHATSAPP_API_URL = "https://graph.facebook.com/v21.0"
OPENAI_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"

# Ritmo de "digitação" simulada
TEMPO_POR_CARACTERE = 0.045   # ~45ms por caractere = ritmo natural
DELAY_MINIMO = 3.0            # nunca menos que 3s
DELAY_MAXIMO = 8.0            # nunca mais que 8s


# ============================================================
# WHATSAPP API
# ============================================================
def marcar_como_lida_e_digitando(phone_number_id, whatsapp_message_id, token=None):
    """
    Marca a mensagem do lead como lida (✓✓ azul) E mostra "Ana está digitando...".
    Se token for None, usa o global como fallback.
    """
    url = f"{WHATSAPP_API_URL}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token or WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": whatsapp_message_id,
        "typing_indicator": {"type": "text"},
    }
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️  Erro ao marcar lida/digitando: {e}")


def enviar_mensagem_whatsapp(phone_number_id, numero_destino, texto, token=None):
    """Envia uma mensagem de texto para o lead. Token específico ou global."""
    url = f"{WHATSAPP_API_URL}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token or WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "text",
        "text": {"body": texto},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"❌ Erro ao enviar mensagem: {e}")
        if "r" in locals():
            print(f"   Resposta da Meta: {r.text}")
        return False


def _normalizar_telefone_br(telefone):
    """Limpa formatação e adiciona 55 se necessário. Retorna só os dígitos."""
    if not telefone:
        return None
    digitos = re.sub(r"\D", "", telefone)
    if not digitos:
        return None
    # Se já começa com 55 e tem mais de 11 dígitos, está OK.
    if digitos.startswith("55") and len(digitos) >= 12:
        return digitos
    # Se tem 10 ou 11 dígitos, adiciona 55 na frente.
    if len(digitos) in (10, 11):
        return "55" + digitos
    return digitos  # devolve o que veio se for outro formato


def notificar_agendamento_para_responsavel(clinica, agendamento_info, numero_lead):
    """
    Envia mensagem no WhatsApp do responsável da clínica avisando do novo agendamento.
    Usa o número que está em clinica['telefone_humano'].
    """
    telefone_responsavel = _normalizar_telefone_br(clinica.get("telefone_humano"))
    if not telefone_responsavel:
        print(f"⚠️ Cliente {clinica['nome']} sem telefone_humano — pulando notificação.")
        return

    data_hora = agendamento_info["agendamento_data_hora"]
    nome_paciente = agendamento_info["agendamento_nome"]
    ag_id = agendamento_info["agendamento_criado_id"]

    data_fmt = _formatar_data_extensa(data_hora)
    hora_fmt = data_hora.strftime("%H:%M")

    texto = (
        f"🗓️ *Novo agendamento marcado pela Ana*\n\n"
        f"*Paciente:* {nome_paciente}\n"
        f"*Telefone:* {numero_lead}\n"
        f"*Data:* {data_fmt}\n"
        f"*Horário:* {hora_fmt}\n\n"
        f"_Ver detalhes e gerenciar no painel Converte.ai_"
    )

    try:
        enviar_mensagem_whatsapp(
            clinica["phone_number_id"], telefone_responsavel, texto,
            token=clinica.get("whatsapp_token")
        )
        print(f"📲 Responsável de {clinica['nome']} notificado sobre agendamento #{ag_id}")
    except Exception as e:
        print(f"❌ Falha ao notificar responsável: {e}")


# ============================================================
# IA (Claude)
# ============================================================
DIAS_SEMANA = [
    "segunda-feira", "terça-feira", "quarta-feira",
    "quinta-feira", "sexta-feira", "sábado", "domingo"
]
MESES = [
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"
]
TIMEZONE_BRASIL = timezone(timedelta(hours=-3))


def _agora_brasil():
    """Retorna o datetime de agora no horário de Brasília (UTC-3)."""
    return datetime.now(TIMEZONE_BRASIL)


def _formatar_data_extensa(dt):
    """Quinta-feira, 18 de junho de 2026"""
    dia_semana = DIAS_SEMANA[dt.weekday()].capitalize()
    mes = MESES[dt.month - 1]
    return f"{dia_semana}, {dt.day} de {mes} de {dt.year}"


def _periodo_do_dia(dt):
    """manhã / tarde / noite / madrugada"""
    h = dt.hour
    if 5 <= h < 12:
        return "manhã"
    if 12 <= h < 18:
        return "tarde"
    if 18 <= h < 24:
        return "noite"
    return "madrugada"


def construir_contexto_temporal():
    """
    Monta um bloco de texto que vai ser injetado no system prompt
    pra Ana ter consciência REAL de data, hora e dia da semana.
    Sem isso ela inventa datas com base nos dados de treinamento.
    """
    agora = _agora_brasil()
    hoje_extenso = _formatar_data_extensa(agora)
    hora = agora.strftime("%H:%M")
    periodo = _periodo_do_dia(agora)

    # Próximos 7 dias pra ela entender quando o lead disser "amanhã", "sexta", etc.
    proximos = []
    for i in range(1, 8):
        d = agora + timedelta(days=i)
        ref = "amanhã" if i == 1 else (
            "depois de amanhã" if i == 2 else DIAS_SEMANA[d.weekday()]
        )
        proximos.append(
            f"  - {ref.capitalize()} = {d.day:02d}/{d.month:02d}/{d.year} "
            f"({DIAS_SEMANA[d.weekday()]})"
        )

    return (
        "\n\n"
        "===== CONTEXTO TEMPORAL ATUAL (USE SEMPRE ESTAS INFORMAÇÕES) =====\n"
        f"HOJE é {hoje_extenso}.\n"
        f"AGORA são {hora} ({periodo} de Brasília).\n"
        "\n"
        "Próximos dias de referência:\n"
        + "\n".join(proximos) +
        "\n\n"
        "REGRAS ABSOLUTAS sobre datas:\n"
        "- NUNCA invente nem chute datas. Use SEMPRE as informações acima.\n"
        "- Quando o lead falar um dia (\"amanhã\", \"sexta\", \"dia 25\"), "
        "use a lista acima pra saber a data exata e a qual mês se refere.\n"
        "- Quando o lead disser só um número (\"dia 31\"), assuma que é o "
        "PRÓXIMO dia 31 a partir de hoje, e confirme natural pelo contexto.\n"
        "- Cumprimentos devem combinar com o período do dia atual "
        "(bom dia / boa tarde / boa noite).\n"
        "================================================================="
    )


# ============================================================
# FERRAMENTAS DE AGENDAMENTO (tool use da API do Claude)
# ============================================================
INSTRUCOES_AGENDAMENTO = """

===== AGENDAMENTO =====
Você tem 3 ferramentas pra agendar consultas:

1. **verificar_disponibilidade** — use SEMPRE antes de propor horários ao lead.
   Nunca chute horários disponíveis. Sempre consulte primeiro.

2. **criar_agendamento** — use SOMENTE quando o lead:
   (a) confirmou explicitamente um horário específico, E
   (b) você já tem o NOME COMPLETO dele.

3. **cancelar_agendamento** — use se o lead quiser cancelar um agendamento que ele tem.

FLUXO IDEAL DE AGENDAMENTO:
1. Lead manifesta interesse em marcar.
2. Pergunta o NOME COMPLETO de forma natural (mesmo que ele já tenha dito o primeiro nome).
   Exemplo: "Pra eu já deixar tudo certinho, qual seu nome completo?"
3. Pergunta a preferência de dia/período (ele pode dizer "amanhã", "sexta de manhã", etc).
4. Chama verificar_disponibilidade pro intervalo apropriado.
5. Propõe 2-3 horários reais ao lead (NUNCA invente horários).
6. Quando o lead confirmar um horário específico, chama criar_agendamento.
7. Confirma pro lead com data, hora e nome.

REGRAS RÍGIDAS:
- NUNCA prometa um horário sem antes verificar disponibilidade.
- NUNCA invente horários ou diga "tenho às 14h" sem ter consultado.
- Se o lead pedir um horário específico, verifique e responda baseado no resultado real.
- Se a ferramenta retornar erro de conflito, peça desculpa e proponha outro horário.
- Sempre confirme o agendamento pro lead com TODOS os detalhes (data por extenso, hora, nome).
========================
"""


FERRAMENTAS = [
    {
        "name": "verificar_disponibilidade",
        "description": (
            "Consulta os horários livres pra agendamento em um intervalo de datas. "
            "Use ANTES de propor horários ao lead. Retorna lista de horários disponíveis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "data_inicio": {
                    "type": "string",
                    "description": "Primeira data do intervalo, formato AAAA-MM-DD. Ex: 2026-06-25"
                },
                "data_fim": {
                    "type": "string",
                    "description": "Última data do intervalo (inclusiva), formato AAAA-MM-DD. Igual a data_inicio se for só um dia."
                }
            },
            "required": ["data_inicio", "data_fim"]
        }
    },
    {
        "name": "criar_agendamento",
        "description": (
            "Marca uma consulta no horário escolhido pelo lead. "
            "Use APENAS depois que o lead confirmou o horário E você tem o nome completo dele."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "data_hora": {
                    "type": "string",
                    "description": "Data e hora do agendamento, formato AAAA-MM-DDTHH:MM. Ex: 2026-06-25T14:00"
                },
                "nome_completo": {
                    "type": "string",
                    "description": "Nome completo do paciente."
                }
            },
            "required": ["data_hora", "nome_completo"]
        }
    },
    {
        "name": "cancelar_agendamento",
        "description": (
            "Cancela um agendamento existente do lead. "
            "Use só se o lead tem um agendamento ativo e está pedindo pra cancelar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agendamento_id": {
                    "type": "integer",
                    "description": "ID do agendamento a cancelar."
                }
            },
            "required": ["agendamento_id"]
        }
    }
]


def _executar_ferramenta(nome, args, clinica, conversa_id, numero_lead):
    """
    Executa a ferramenta que a Ana chamou e retorna o resultado (string) pra ela.
    Retorna também um dicionário com info extra (ex: id do agendamento criado).
    """
    extra = {}

    if nome == "verificar_disponibilidade":
        try:
            data_ini = datetime.strptime(args["data_inicio"], "%Y-%m-%d").date()
            data_fim = datetime.strptime(args["data_fim"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            return "Erro: formato de data inválido. Use AAAA-MM-DD.", extra

        if (data_fim - data_ini).days > 14:
            return "Erro: intervalo muito longo. Consulte no máximo 14 dias por vez.", extra

        slots = obter_horarios_disponiveis_intervalo(
            clinica["id"], data_ini, data_fim, max_por_dia=4
        )

        if not slots:
            return (
                f"Nenhum horário disponível entre {data_ini.strftime('%d/%m')} "
                f"e {data_fim.strftime('%d/%m')}. Tente um intervalo diferente "
                f"ou verifique se a clínica atende nesses dias da semana."
            ), extra

        # Formata pra Ana entender melhor
        linhas = []
        dia_atual = None
        for s in slots:
            d = s.date()
            if d != dia_atual:
                dia_atual = d
                dia_str = f"{DIAS_SEMANA[d.weekday()]} ({d.strftime('%d/%m')})"
                linhas.append(f"\n{dia_str.capitalize()}:")
            linhas.append(f"  - {s.strftime('%H:%M')} (use isso em criar_agendamento: {s.strftime('%Y-%m-%dT%H:%M')})")

        return "Horários disponíveis:" + "".join(linhas), extra

    elif nome == "criar_agendamento":
        try:
            dt = datetime.strptime(args["data_hora"], "%Y-%m-%dT%H:%M")
            from datetime import timezone, timedelta
            dt = dt.replace(tzinfo=timezone(timedelta(hours=-3)))
        except (ValueError, KeyError):
            return "Erro: formato de data/hora inválido. Use AAAA-MM-DDTHH:MM.", extra

        nome_paciente = args.get("nome_completo", "").strip()
        if not nome_paciente or len(nome_paciente) < 3:
            return "Erro: nome completo é obrigatório.", extra

        try:
            ag_id = criar_agendamento(
                clinica_id=clinica["id"],
                numero_lead=numero_lead,
                data_hora=dt,
                nome_lead=nome_paciente,
                conversa_id=conversa_id,
                origem="ana"
            )
            extra["agendamento_criado_id"] = ag_id
            extra["agendamento_data_hora"] = dt
            extra["agendamento_nome"] = nome_paciente

            data_fmt = _formatar_data_extensa(dt)
            hora_fmt = dt.strftime("%H:%M")
            return (
                f"Agendamento confirmado com sucesso. ID {ag_id}. "
                f"Paciente: {nome_paciente}. Data: {data_fmt} às {hora_fmt}. "
                f"Confirme isso ao lead de forma natural na próxima resposta."
            ), extra
        except ValueError as e:
            if "ocupado" in str(e):
                return (
                    "Erro: esse horário acabou de ficar ocupado. "
                    "Peça desculpa ao lead e proponha outro horário "
                    "(use verificar_disponibilidade de novo)."
                ), extra
            return f"Erro ao criar: {e}", extra
        except Exception as e:
            return f"Erro técnico ao criar agendamento: {e}", extra

    elif nome == "cancelar_agendamento":
        try:
            ag_id = int(args["agendamento_id"])
        except (ValueError, KeyError, TypeError):
            return "Erro: ID inválido.", extra

        ag = obter_agendamento(ag_id)
        if not ag:
            return "Erro: agendamento não encontrado.", extra
        # Segurança: só cancela agendamentos da mesma clínica E do mesmo lead
        if ag["clinica_id"] != clinica["id"] or ag["numero_lead"] != numero_lead:
            return "Erro: esse agendamento não pertence a esse contato.", extra

        if cancelar_agendamento(ag_id):
            return "Agendamento cancelado com sucesso. Confirme ao lead.", extra
        return "Erro: agendamento já estava cancelado ou não pôde ser cancelado.", extra

    return f"Erro: ferramenta '{nome}' desconhecida.", extra


def _formatar_config_horarios_pro_prompt(clinica_id):
    """Injeta no prompt a configuração de horários da clínica."""
    cfg = obter_config_horarios(clinica_id)
    dias_map = {1: "segunda", 2: "terça", 3: "quarta", 4: "quinta",
                5: "sexta", 6: "sábado", 7: "domingo"}
    dias_atende = [dias_map[int(d)] for d in cfg["dias_semana"].split(",")]
    txt = (
        f"\n\n===== HORÁRIOS DE ATENDIMENTO DESTA CLÍNICA =====\n"
        f"- Dias: {', '.join(dias_atende)}\n"
        f"- Horário: {cfg['hora_inicio'].strftime('%H:%M')} às {cfg['hora_fim'].strftime('%H:%M')}\n"
        f"- Duração de cada consulta: {cfg['duracao_minutos']} minutos\n"
        f"- Antecedência mínima pra agendamento: {cfg['antecedencia_minima_minutos']} minutos\n"
    )
    if cfg.get("almoco_inicio") and cfg.get("almoco_fim"):
        txt += f"- Pausa: {cfg['almoco_inicio'].strftime('%H:%M')} às {cfg['almoco_fim'].strftime('%H:%M')}\n"
    txt += "================================================="
    return txt


def gerar_resposta_ia(system_prompt, historico, mensagem_atual,
                     clinica=None, conversa_id=None, numero_lead=None):
    """
    Chama a API do Claude e retorna o texto da resposta.
    Agora suporta tool use: a Ana pode chamar ferramentas de agendamento.

    Retorna uma tupla (texto_resposta, lista_de_agendamentos_criados).
    """
    messages = list(historico)
    messages.append({"role": "user", "content": mensagem_atual})

    # Injeta contexto temporal + config de horários + instruções de agendamento
    system_completo = system_prompt + construir_contexto_temporal()
    if clinica:
        system_completo += _formatar_config_horarios_pro_prompt(clinica["id"])
        system_completo += INSTRUCOES_AGENDAMENTO

    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    agendamentos_criados = []
    # Loop de tool use — Ana pode chamar ferramentas múltiplas vezes
    # até dar a resposta final em texto.
    for tentativa in range(5):  # limite de segurança
        payload = {
            "model": CLAUDE_MODEL,
            "max_tokens": 1024,
            "temperature": 0.7,
            "system": system_completo,
            "messages": messages,
        }
        # Só habilita ferramentas se a clínica foi passada
        if clinica and conversa_id and numero_lead:
            payload["tools"] = FERRAMENTAS

        try:
            r = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=45)
            if r.status_code != 200:
                print(f"❌ Claude retornou {r.status_code}: {r.text}")
                return None, agendamentos_criados
            data = r.json()
        except Exception as e:
            print(f"❌ Erro ao chamar Claude: {e}")
            return None, agendamentos_criados

        stop_reason = data.get("stop_reason")
        content = data.get("content", [])

        # Se ela usou ferramenta(s), processa e continua o loop
        if stop_reason == "tool_use":
            # Adiciona a resposta dela ao histórico
            messages.append({"role": "assistant", "content": content})

            # Processa cada tool_use
            tool_results = []
            for bloco in content:
                if bloco.get("type") != "tool_use":
                    continue
                nome_tool = bloco["name"]
                tool_id = bloco["id"]
                args = bloco.get("input", {})
                print(f"🔧 Ana chamou: {nome_tool}({args})")
                resultado, extra = _executar_ferramenta(
                    nome_tool, args, clinica, conversa_id, numero_lead
                )
                print(f"   → {resultado[:120]}")
                if "agendamento_criado_id" in extra:
                    agendamentos_criados.append(extra)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": resultado,
                })

            # Adiciona os resultados das ferramentas como mensagem do usuário
            messages.append({"role": "user", "content": tool_results})
            # Continua o loop pra Ana gerar a resposta final
            continue

        # Resposta final em texto
        textos = [b["text"] for b in content if b.get("type") == "text"]
        return ("\n".join(textos).strip(), agendamentos_criados)

    # Se passou de 5 tentativas, algo deu errado
    print("⚠️ Loop de tool use excedeu 5 tentativas.")
    return None, agendamentos_criados


# ============================================================
# ÁUDIO (download do WhatsApp + transcrição via OpenAI Whisper)
# ============================================================
def baixar_audio_whatsapp(media_id, token=None):
    """
    Baixa o arquivo de áudio do WhatsApp em duas etapas:
    1) pede a URL temporária do arquivo (autenticada)
    2) baixa o conteúdo binário dessa URL
    Retorna os bytes do áudio, ou None em caso de erro.
    """
    headers = {"Authorization": f"Bearer {token or WHATSAPP_TOKEN}"}
    try:
        # Etapa 1: pega a URL temporária
        r1 = requests.get(
            f"{WHATSAPP_API_URL}/{media_id}",
            headers=headers, timeout=10
        )
        r1.raise_for_status()
        url_arquivo = r1.json().get("url")
        if not url_arquivo:
            print("⚠️  WhatsApp não retornou URL do áudio.")
            return None

        # Etapa 2: baixa o conteúdo
        r2 = requests.get(url_arquivo, headers=headers, timeout=30)
        r2.raise_for_status()
        return r2.content
    except Exception as e:
        print(f"❌ Erro ao baixar áudio: {e}")
        return None


def transcrever_audio(audio_bytes):
    """
    Manda o áudio pra OpenAI Whisper e retorna o texto transcrito.
    Usa language='pt' pra otimizar pra português.
    """
    if not audio_bytes:
        return None

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    # WhatsApp manda áudio em OGG/Opus — Whisper aceita direto.
    files = {"file": ("audio.ogg", audio_bytes, "audio/ogg")}
    data = {"model": "whisper-1", "language": "pt"}

    try:
        r = requests.post(
            OPENAI_TRANSCRIBE_URL,
            headers=headers, files=files, data=data, timeout=60
        )
        if r.status_code != 200:
            print(f"❌ Whisper retornou {r.status_code}: {r.text}")
            return None
        texto = r.json().get("text", "").strip()
        return texto if texto else None
    except Exception as e:
        print(f"❌ Erro ao transcrever áudio: {e}")
        return None


# ============================================================
# QUEBRA DE RESPOSTA EM PARTES
# ============================================================
def quebrar_em_partes(texto):
    """
    Quebra a resposta em mensagens curtas — como uma pessoa real no WhatsApp.
    Limita a no máximo 3 partes pra não virar metralhadora.
    """
    texto = texto.strip()

    # Resposta curta vai inteira.
    if len(texto) <= 120:
        return [texto]

    # Se a Ana usou quebra de linha propositalmente, respeitamos.
    partes_por_linha = [p.strip() for p in texto.split("\n") if p.strip()]
    if len(partes_por_linha) >= 2:
        return partes_por_linha[:3]

    # Quebra por frases (.!?).
    frases = re.split(r"(?<=[.!?])\s+", texto)
    frases = [f.strip() for f in frases if f.strip()]
    if len(frases) <= 1:
        return [texto]

    # Agrupa frases em blocos de ~120 caracteres.
    partes = []
    parte_atual = ""
    for frase in frases:
        if parte_atual and len(parte_atual) + len(frase) + 1 > 120:
            partes.append(parte_atual.strip())
            parte_atual = frase
        else:
            parte_atual = (parte_atual + " " + frase).strip()
    if parte_atual:
        partes.append(parte_atual.strip())

    # Limita a 3 partes (junta o que sobrar na última).
    if len(partes) > 3:
        partes = partes[:2] + [" ".join(partes[2:])]

    return partes


def calcular_delay(texto):
    """Tempo proporcional ao tamanho da resposta, com leve aleatoriedade."""
    base = len(texto) * TEMPO_POR_CARACTERE
    base += random.uniform(0.5, 1.5)
    return max(DELAY_MINIMO, min(DELAY_MAXIMO, base))


# ============================================================
# PROCESSAMENTO EM SEGUNDO PLANO
# ============================================================
def processar_mensagem_em_background(
    clinica, conversa_id, message_id_whatsapp, numero_lead,
    texto_recebido=None, audio_media_id=None
):
    """
    Roda em uma thread separada — o webhook já respondeu 200 pra Meta,
    então aqui podemos tomar nosso tempo: digitar, pensar, responder.

    Se vier áudio (audio_media_id), transcreve primeiro e usa o texto resultante.
    """
    try:
        # Token específico da clínica (ou None = usa o global como fallback).
        token_clinica = clinica.get("whatsapp_token")

        # 1) Mostra check azul + "digitando..." imediatamente
        marcar_como_lida_e_digitando(
            clinica["phone_number_id"], message_id_whatsapp,
            token=token_clinica
        )

        # 1.5) Se for áudio, baixa e transcreve.
        if audio_media_id:
            print(f"🎵 Transcrevendo áudio de {numero_lead}...")
            audio_bytes = baixar_audio_whatsapp(audio_media_id, token=token_clinica)
            texto_recebido = transcrever_audio(audio_bytes)

            if not texto_recebido:
                # Não conseguiu transcrever — avisa o lead e desiste dessa mensagem.
                enviar_mensagem_whatsapp(
                    clinica["phone_number_id"], numero_lead,
                    "Não consegui entender o áudio direito. "
                    "Pode tentar de novo ou me escrever?",
                    token=token_clinica
                )
                return

            print(f"   Transcrição: {texto_recebido}")

        # 2) Salva a mensagem do lead no banco (texto, mesmo se veio de áudio).
        salvar_mensagem(
            conversa_id, "user", texto_recebido,
            message_id_whatsapp=message_id_whatsapp
        )

        # 2.5) Se um humano assumiu a conversa, a Ana NÃO responde.
        # A mensagem fica salva pra ele ler no painel.
        if conversa_esta_pausada(conversa_id):
            print(f"⏸️  Conversa {conversa_id} pausada (humano assumiu) — Ana em silêncio.")
            return

        # 3) Carrega o histórico recente da conversa.
        historico = obter_historico_conversa(conversa_id, limite=20)
        # A última mensagem do histórico É a que acabou de chegar.
        # A função gerar_resposta_ia vai re-adicionar ela, então tiramos daqui.
        if historico and historico[-1]["role"] == "user":
            historico_base = historico[:-1]
        else:
            historico_base = historico

        # 4) Gera resposta com Claude. Passa o contexto pra Ana poder agendar.
        resposta_completa, agendamentos = gerar_resposta_ia(
            clinica["system_prompt"], historico_base, texto_recebido,
            clinica=clinica, conversa_id=conversa_id, numero_lead=numero_lead
        )

        if not resposta_completa:
            telefone_humano = clinica.get("telefone_humano") or ""
            fallback = "Desculpe, tive um problema técnico. Pode tentar novamente"
            fallback += f" ou ligar no {telefone_humano}?" if telefone_humano else "?"
            enviar_mensagem_whatsapp(
                clinica["phone_number_id"], numero_lead, fallback,
                token=token_clinica
            )
            return

        # 5) Salva a resposta da Ana inteira no banco (uma vez).
        salvar_mensagem(conversa_id, "assistant", resposta_completa)

        # 6) Quebra em partes e envia cada uma com delay natural.
        partes = quebrar_em_partes(resposta_completa)
        for parte in partes:
            time.sleep(calcular_delay(parte))
            enviar_mensagem_whatsapp(
                clinica["phone_number_id"], numero_lead, parte,
                token=token_clinica
            )

        print(f"💬 [{clinica['nome']}] Ana respondeu em {len(partes)} parte(s) pra {numero_lead}")

        # 7) Se a Ana agendou algo nessa rodada, notifica o responsável da clínica.
        for ag in agendamentos:
            notificar_agendamento_para_responsavel(clinica, ag, numero_lead)

    except Exception as e:
        print(f"❌ Erro no processamento de background: {e}")


# ============================================================
# ROTAS
# ============================================================
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Verificação inicial do webhook pela Meta."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verificado!")
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def receive_message():
    """
    Recebe a mensagem da Meta e responde 200 IMEDIATAMENTE.
    O processamento real (Claude + envio) roda em thread separada.
    Isso evita que a Meta reenvie webhooks por timeout.
    """
    try:
        data = request.get_json()

        if data.get("object") != "whatsapp_business_account":
            return jsonify({"status": "ignored"}), 200

        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # MULTI-TENANT: descobre qual clínica recebeu a mensagem.
                metadata = value.get("metadata", {})
                phone_number_id = metadata.get("phone_number_id")
                if not phone_number_id:
                    continue

                clinica = buscar_clinica_por_phone_id(phone_number_id)
                if not clinica:
                    print(f"⚠️  Sem clínica cadastrada pra phone_id {phone_number_id}.")
                    continue

                for message in value.get("messages", []):
                    message_id = message.get("id")
                    sender = message.get("from")
                    msg_type = message.get("type")

                    # Anti-duplicata (consulta o banco).
                    if mensagem_ja_processada(message_id):
                        print(f"⏭️  Mensagem {message_id} já processada.")
                        continue

                    if msg_type == "text":
                        texto = message.get("text", {}).get("body", "")
                        print(f"\n📨 [{clinica['nome']}] {sender}: {texto}")

                        conversa_id = obter_ou_criar_conversa(
                            clinica["id"], sender
                        )

                        # Dispara processamento em segundo plano (texto).
                        threading.Thread(
                            target=processar_mensagem_em_background,
                            kwargs={
                                "clinica": dict(clinica),
                                "conversa_id": conversa_id,
                                "message_id_whatsapp": message_id,
                                "numero_lead": sender,
                                "texto_recebido": texto,
                            },
                            daemon=True
                        ).start()

                    elif msg_type == "audio":
                        audio_info = message.get("audio", {})
                        media_id = audio_info.get("id")
                        if not media_id:
                            print(f"⚠️  Áudio sem media_id, ignorando.")
                            continue

                        print(f"\n🎵 [{clinica['nome']}] áudio de {sender} (media_id={media_id})")

                        conversa_id = obter_ou_criar_conversa(
                            clinica["id"], sender
                        )

                        # Dispara processamento em segundo plano (áudio → transcrição → resposta).
                        threading.Thread(
                            target=processar_mensagem_em_background,
                            kwargs={
                                "clinica": dict(clinica),
                                "conversa_id": conversa_id,
                                "message_id_whatsapp": message_id,
                                "numero_lead": sender,
                                "audio_media_id": media_id,
                            },
                            daemon=True
                        ).start()

                    else:
                        print(f"ℹ️  Tipo '{msg_type}' não tratado.")
                        enviar_mensagem_whatsapp(
                            phone_number_id, sender,
                            "Por enquanto consigo ler apenas mensagens de texto e áudio. "
                            "Pode me escrever?",
                            token=clinica.get("whatsapp_token")
                        )

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ Erro no webhook: {e}")
        # Mesmo em erro, responde 200 — não queremos a Meta reenviando.
        return jsonify({"status": "error"}), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "online",
        "service": "WhatsApp Ana (multi-tenant)",
        "ai": CLAUDE_MODEL,
    })


# ============================================================
# INICIALIZAÇÃO DO BANCO + REGISTRO DO PAINEL
# ============================================================
try:
    inicializar_banco()
    print("✅ Banco de dados pronto.")
except Exception as e:
    print(f"⚠️  Erro ao inicializar banco: {e}")

# Anexa todas as rotas do painel web ao app principal.
registrar_rotas_painel(app)
print("✅ Painel web registrado em /painel")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

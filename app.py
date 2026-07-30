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
import copy
from datetime import datetime, timedelta, timezone

from db import (
    inicializar_banco,
    buscar_clinica_por_phone_id,
    buscar_clinica_por_telefone_humano,
    obter_ou_criar_conversa,
    salvar_referral_conversa,
    obter_conversa,
    buscar_conversa_por_numero,
    registrar_venda,
    mensagem_ja_processada,
    salvar_mensagem,
    obter_historico_conversa,
    conversa_esta_pausada,
    obter_horarios_disponiveis,
    obter_horarios_disponiveis_intervalo,
    criar_agendamento,
    cancelar_agendamento,
    remarcar_agendamento,
    obter_agendamento,
    buscar_agendamentos_ativos_lead,
    obter_nome_lead,
    obter_config_horarios,
    listar_agendamentos,
    criar_bloqueio,
    remover_bloqueio,
    listar_bloqueios,
    listar_profissionais,
    followup_claim,
    followup_resultado,
    marcar_optout_conversa,
    listar_agendamentos_lembrete,
    listar_conversas_frio_t1,
    listar_conversas_frio_t2,
)
from painel import registrar_rotas as registrar_rotas_painel
import capi

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
# Nome do template aprovado na Meta pra notificar o responsável da clínica
# sobre agendamentos (foge da janela de 24h que mensagem de texto livre exige).
TEMPLATE_NOTIFICACAO_AGENDAMENTO = os.getenv("TEMPLATE_NOTIFICACAO_AGENDAMENTO", "notificacao_agendamento_ana")

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


def enviar_template_whatsapp(phone_number_id, numero_destino, template_name, parametros_body, token=None):
    """
    Envia mensagem via template aprovado da Meta (HSM).
    Diferente de mensagem de texto livre, o template ignora a janela de 24h de
    atendimento — funciona mesmo que o destinatário não tenha mandado mensagem
    recente pro número. Necessário pra notificar o dono da clínica, que
    normalmente não conversa direto com o número da Ana.
    """
    url = f"{WHATSAPP_API_URL}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token or WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    template = {
        "name": template_name,
        "language": {"code": "pt_BR"},
    }
    # Só inclui o componente de corpo se o template tem variáveis. Template sem
    # variáveis (ex: lead frio genérico) não pode receber 'parameters' vazio.
    if parametros_body:
        template["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in parametros_body],
        }]
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "template",
        "template": template,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"❌ Erro ao enviar template '{template_name}': {e}")
        if "r" in locals():
            print(f"   Resposta da Meta: {r.text}")
        return False


def _telefone_canonico(numero):
    """
    Reduz um telefone BR a DDD + 8 dígitos, ignorando o DDI (55) e o 9º dígito
    do celular — que variam entre como o número é salvo e como o WhatsApp o envia.
    Usado pra reconhecer o DONO da clínica de forma robusta (evita falhar por causa
    do nono dígito). Retorna 10 dígitos (DDD + assinante) ou None.
    """
    d = re.sub(r"\D", "", numero or "")
    if not d:
        return None
    if len(d) >= 12 and d.startswith("55"):
        d = d[2:]            # tira o DDI
    if len(d) == 11 and d[2] == "9":
        d = d[:2] + d[3:]    # tira o 9º dígito do celular
    return d[-10:] if len(d) >= 10 else d


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
    tel_bruto = clinica.get("telefone_humano")
    print(f"📲 Tentando notificar responsável. Telefone bruto: '{tel_bruto}'")
    telefone_responsavel = _normalizar_telefone_br(tel_bruto)
    print(f"📲 Telefone normalizado: '{telefone_responsavel}'")

    if not telefone_responsavel:
        print(f"⚠️ Cliente {clinica['nome']} sem telefone_humano válido — pulando notificação.")
        return

    data_hora = agendamento_info["agendamento_data_hora"]
    nome_paciente = agendamento_info["agendamento_nome"]
    ag_id = agendamento_info["agendamento_criado_id"]
    motivo = agendamento_info.get("agendamento_observacao") or "não informado"
    tipo = agendamento_info.get("agendamento_tipo", "novo")

    data_fmt = _formatar_data_extensa(data_hora)
    hora_fmt = data_hora.strftime("%H:%M")

    tipo_label = "Novo" if tipo == "novo" else "Remarcado"
    titulo = (
        "🗓️ *Novo agendamento marcado pela Ana*" if tipo == "novo"
        else "🔁 *Agendamento remarcado pela Ana*"
    )
    texto = (
        f"{titulo}\n\n"
        f"*Paciente:* {nome_paciente}\n"
        f"*Telefone:* {numero_lead}\n"
        f"*Data:* {data_fmt}\n"
        f"*Horário:* {hora_fmt}\n"
        f"*Motivo:* {motivo}\n\n"
        f"_Ver detalhes e gerenciar no painel Converte.ai_"
    )

    print(f"📲 Enviando notificação (template) pra {telefone_responsavel} via phone_id {clinica['phone_number_id']}")
    try:
        # Mensagem de texto livre só entrega se o responsável tiver mandado
        # mensagem pro número da Ana nas últimas 24h — o que raramente acontece
        # (dono não costuma conversar direto com a Ana). Por isso usamos template
        # aprovado primeiro, que ignora essa janela. Texto livre fica só de fallback
        # pro caso raro da janela estar aberta e o template ainda não ter sido aprovado.
        sucesso = enviar_template_whatsapp(
            clinica["phone_number_id"], telefone_responsavel,
            TEMPLATE_NOTIFICACAO_AGENDAMENTO,
            [tipo_label, nome_paciente, numero_lead, data_fmt, hora_fmt, motivo],
            token=clinica.get("whatsapp_token"),
        )
        if not sucesso:
            print("⚠️ Template falhou — tentando texto livre como fallback.")
            sucesso = enviar_mensagem_whatsapp(
                clinica["phone_number_id"], telefone_responsavel, texto,
                token=clinica.get("whatsapp_token")
            )
        if sucesso:
            print(f"✅ Responsável de {clinica['nome']} notificado sobre agendamento #{ag_id}")
        else:
            print(f"❌ Notificação falhou (template e texto livre) — verifique log acima.")
    except Exception as e:
        print(f"❌ Exceção ao notificar responsável: {e}")
        import traceback
        traceback.print_exc()


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


def _formatar_data_lembrete(dt):
    """Pro lembrete de confirmação: 'quinta-feira, 30 de julho' (minúsculo e sem
    ano — a consulta é sempre 'hoje', então o ano fica redundante na frase)."""
    return f"{DIAS_SEMANA[dt.weekday()]}, {dt.day} de {MESES[dt.month - 1]}"


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
Você tem estas ferramentas pra agendar consultas:

1. **verificar_disponibilidade** — use SEMPRE antes de propor horários ao lead.
   Nunca chute horários disponíveis. Sempre consulte primeiro.

2. **criar_agendamento** — use SOMENTE quando o lead:
   (a) confirmou explicitamente um horário específico, E
   (b) você já tem o NOME COMPLETO dele.

3. **cancelar_agendamento** — use se o lead quiser cancelar (sem remarcar) um agendamento que ele tem.

4. **consultar_meu_agendamento** — use pra descobrir o ID do(s) agendamento(s) ativo(s)
   desse contato, quando o lead pedir pra remarcar ou cancelar e o ID não estiver claro
   pelo histórico da conversa.

5. **remarcar_agendamento** — use quando o lead JÁ TEM um agendamento e quer MUDAR o
   dia/horário dele. NÃO use cancelar_agendamento + criar_agendamento pra isso.

FLUXO IDEAL DE AGENDAMENTO:
1. Lead manifesta interesse em marcar.
2. Pergunta o NOME COMPLETO de forma natural, UMA ÚNICA VEZ.
   Exemplo: "Pra eu já deixar tudo certinho, qual seu nome completo?"
   - Se o nome deste contato JÁ apareceu na conversa OU está informado no bloco
     "PACIENTE JÁ IDENTIFICADO", NÃO pergunte de novo — use o nome que você já tem.
   - Perguntar o nome mais de uma vez pra mesma pessoa (ex: ao remarcar) é um erro
     grave: irrita o lead e pode fazer ele desistir. Só pergunte de novo se for
     claramente outra pessoa (ex: marcando pra um familiar).
3. Pergunta a preferência de dia/período (ele pode dizer "amanhã", "sexta de manhã", etc).
4. Chama verificar_disponibilidade pro intervalo apropriado.
5. Propõe horários reais ao lead. Se a lista de disponíveis tiver muitos horários,
   escolhe 4-5 espalhados ao longo do dia (manhã, meio-dia, tarde) pra não inundar.
   Sempre deixa CLARO que se nenhum servir, ele pode pedir outros horários
   ("se preferir outro horário, me avisa que vejo aqui").
   Quando o lead perguntar "só tem esses?", você LISTA TODOS os disponíveis dessa vez.
6. Quando o lead confirmar um horário específico, chama criar_agendamento.
7. Confirma pro lead com data, hora e nome.

MOTIVO/TRATAMENTO DO AGENDAMENTO:
- Antes de chamar criar_agendamento, releia a conversa: é muito provável que o lead já
  tenha dito em algum momento qual tratamento busca (ex: avaliação, clareamento,
  harmonização, implante, etc), mesmo sem você perguntar diretamente.
- Se o motivo já apareceu em qualquer ponto da conversa, USE ESSE MOTIVO no campo
  `observacao` da ferramenta. NÃO pergunte de novo — perguntar algo que o lead já
  disse soa como se você não tivesse prestado atenção.
- Só pergunte o motivo diretamente, de forma natural, se ele REALMENTE nunca apareceu
  em nenhuma mensagem anterior. Exemplo: "Só pra eu já deixar registrado, é pra qual
  tratamento?"
- Preencha `observacao` de forma curta (ex: "Interesse em clareamento"). Se mesmo assim
  não conseguir identificar, pode chamar criar_agendamento sem preencher esse campo.

FLUXO DE REAGENDAMENTO (lead já tem consulta marcada e quer mudar dia/horário):
1. Identifique o ID do agendamento atual. Se apareceu antes na conversa (na mensagem de
   confirmação de quando foi marcado), use esse ID direto. Se não tiver certeza, chame
   consultar_meu_agendamento primeiro pra descobrir.
2. Pergunta a nova preferência de dia/período e chama verificar_disponibilidade
   normalmente pro novo intervalo, do mesmo jeito que faria pra um agendamento novo.
3. Quando o lead confirmar o novo horário, chame remarcar_agendamento passando o ID
   do agendamento e a nova data/hora. NUNCA use cancelar_agendamento seguido de
   criar_agendamento pra isso — remarcar_agendamento já move o mesmo registro mantendo
   nome e motivo, e cancelar perderia essas informações à toa.
4. Confirme ao lead a mudança com a nova data e horário por extenso.
5. Se remarcar_agendamento retornar erro de horário ocupado, peça desculpa e proponha
   outro (use verificar_disponibilidade de novo).

REGRAS RÍGIDAS:
- NUNCA prometa um horário sem antes verificar disponibilidade.
- NUNCA invente horários ou diga "tenho às 14h" sem ter consultado.
- Se o lead pedir um horário específico, verifique e responda baseado no resultado real.
- Se o horário pedido pelo lead NÃO estiver na lista de disponíveis, diga que aquele
  horário específico não está livre e proponha os mais próximos.
- Só é possível marcar EXATAMENTE nos horários que a ferramenta retornou (ex: 14:00,
  15:00). Se o lead pedir um horário quebrado ou fora da lista (ex: 16:30 quando a lista
  tem só horas cheias), NÃO aceite nem confirme esse horário — explique que trabalha com
  aqueles horários e ofereça o mais próximo da lista.
- Se a ferramenta retornar erro de conflito, peça desculpa e proponha outro horário.

REGRA ABSOLUTA DE CONFIRMAÇÃO (a mais importante):
- Você SÓ pode dizer que o agendamento está marcado/confirmado DEPOIS de chamar
  criar_agendamento E receber de volta a confirmação com o ID. Enquanto você não
  chamar a ferramenta e ela não confirmar, o agendamento NÃO EXISTE.
- É TERMINANTEMENTE PROIBIDO dizer "agendado", "marcado", "confirmado", "está reservado"
  ou qualquer coisa parecida sem ter chamado criar_agendamento e recebido o sucesso.
  Dizer que marcou sem ter marcado faz o paciente aparecer e não ter horário — é o pior
  erro possível.
- Fluxo obrigatório sempre: (1) lead confirma um horário da lista → (2) você chama
  criar_agendamento → (3) a ferramenta retorna sucesso com ID → (4) SÓ ENTÃO você
  confirma pro lead com data por extenso, hora e nome.
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
                },
                "observacao": {
                    "type": "string",
                    "description": (
                        "Motivo/tratamento pelo qual o lead quer a consulta (ex: 'Interesse em "
                        "clareamento', 'Avaliação', 'Harmonização facial'). Preencha com o que já "
                        "foi dito na conversa — só pergunte diretamente se nunca apareceu. Pode "
                        "deixar vazio se não foi possível identificar."
                    )
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
    },
    {
        "name": "consultar_meu_agendamento",
        "description": (
            "Consulta o(s) agendamento(s) ativo(s) do lead atual nesta clínica. Use quando "
            "o lead quiser remarcar ou cancelar mas o ID do agendamento não estiver claro "
            "pelo histórico da conversa. Não precisa de parâmetros."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "remarcar_agendamento",
        "description": (
            "Move um agendamento existente do lead pra um novo dia/horário, mantendo o "
            "mesmo registro (nome, motivo). Use quando o lead quiser MUDAR o horário de "
            "uma consulta já marcada, em vez de cancelar_agendamento + criar_agendamento."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agendamento_id": {
                    "type": "integer",
                    "description": "ID do agendamento a remarcar."
                },
                "nova_data_hora": {
                    "type": "string",
                    "description": "Nova data e hora, formato AAAA-MM-DDTHH:MM. Ex: 2026-06-25T15:00"
                }
            },
            "required": ["agendamento_id", "nova_data_hora"]
        }
    }
]


# ============================================================
# MULTI-PROFISSIONAL (usado só quando a clínica tem profissionais)
# ============================================================
def _formatar_profissionais_pro_prompt(profissionais):
    """
    Bloco injetado no prompt da Ana listando os profissionais (id + nome).
    A Ana usa o ID pra chamar verificar_disponibilidade e criar_agendamento.
    O roteamento por especialidade vive no prompt de cada clínica (aqui só o mapa id→nome).
    """
    if not profissionais:
        return ""
    linhas = "\n".join(f"- ID {p['id']}: {p['nome']}" for p in profissionais)
    return (
        "\n\n===== PROFISSIONAIS DESTA CLÍNICA (AGENDA SEPARADA POR PROFISSIONAL) =====\n"
        "Esta clínica tem mais de um profissional, cada um com AGENDA e horários próprios.\n"
        "Profissionais cadastrados (use o ID EXATO ao agendar):\n"
        f"{linhas}\n"
        "\n"
        "REGRAS DE PROFISSIONAL (siga sempre):\n"
        "- Descubra pelo que o lead procura QUAL profissional cuida do caso. As "
        "especialidades de cada um estão descritas mais acima no seu prompt.\n"
        "- SEMPRE informe o profissional_id correto em verificar_disponibilidade e "
        "criar_agendamento. Disponibilidade e marcação são na agenda DAQUELE profissional.\n"
        "- NUNCA marque no profissional errado. Se não estiver claro qual profissional "
        "atende o que o lead quer, pergunte de forma natural o que ele procura ANTES de "
        "verificar horários.\n"
        "- Pra remarcar ou cancelar você NÃO precisa do profissional_id: o sistema já "
        "sabe de qual profissional é aquele agendamento.\n"
        "=================================================================="
    )


def _ferramentas_lead(tem_profissionais):
    """
    Retorna a lista de ferramentas do modo lead. Em clínica multi-profissional,
    injeta o parâmetro profissional_id (obrigatório) em verificar_disponibilidade
    e criar_agendamento. Em clínica single, retorna as ferramentas originais.
    """
    if not tem_profissionais:
        return FERRAMENTAS
    tools = copy.deepcopy(FERRAMENTAS)
    for t in tools:
        if t["name"] in ("verificar_disponibilidade", "criar_agendamento"):
            t["input_schema"]["properties"]["profissional_id"] = {
                "type": "integer",
                "description": (
                    "ID do profissional cuja agenda usar. Precisa ser um dos IDs "
                    "listados na seção PROFISSIONAIS DESTA CLÍNICA, escolhido pela "
                    "especialidade que o lead procura."
                )
            }
            if "profissional_id" not in t["input_schema"]["required"]:
                t["input_schema"]["required"].append("profissional_id")
    return tools


def _resolver_profissional_lead(args, clinica):
    """
    Em clínica multi-profissional: valida o profissional_id que a Ana passou.
    Retorna (profissional_id, erro_str):
      - clínica single (sem profissionais): (None, None) — segue o fluxo antigo.
      - multi com id válido: (id, None).
      - multi com id ausente/inválido: (None, mensagem de erro pra Ana corrigir).
    """
    profs = listar_profissionais(clinica["id"])
    if not profs:
        return None, None
    ids = {p["id"] for p in profs}
    pid = args.get("profissional_id")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        pid = None
    if pid not in ids:
        nomes = ", ".join(f"{p['nome']} = ID {p['id']}" for p in profs)
        return None, (
            "Erro: esta clínica tem mais de um profissional, cada um com agenda "
            f"própria ({nomes}). Você precisa informar o profissional_id correto. "
            "Escolha pela especialidade que o lead procura e chame a ferramenta de "
            "novo com o profissional_id certo."
        )
    return pid, None


# ============================================================
# MODO DONO — o responsável da clínica falando com a Ana
# ============================================================
PROMPT_MODO_DONO = """Você é Ana, mas agora está conversando com o RESPONSÁVEL da clínica (o dono, o profissional, ou alguém da equipe administrativa) — NÃO com um paciente.

Seu papel agora é de ASSISTENTE EXECUTIVA. O dono usa você pra:
- Bloquear horários quando ele não vai estar disponível (cirurgia longa, viagem, compromisso pessoal)
- Registrar manualmente um paciente que marcou por outro canal (telefone direto, presencial)
- Cancelar bloqueios anteriores
- Consultar o que está marcado em determinado período
- Desbloquear horários
- Registrar uma VENDA quando ele fechar negócio com um lead (ex: "fechei com o fulano, 3 mil")

TOM: direto, eficiente, sem rodeios. Sem "consultar nossa equipe", sem "vou verificar", sem floreios. Confirma a ação rapidamente.

Exemplos do tom esperado:
- "Bloqueado das 14h às 16h amanhã. Motivo registrado: cirurgia longa."
- "Maria Silva marcada pra sexta 25/06 às 10h. Confirmação automática enviada."
- "Você tem 3 agendamentos amanhã: 9h João, 10h Carlos, 14h Patricia."
- "Bloqueio das 14h removido."

REGRAS:
- Não invente dados. Use as ferramentas pra tudo.
- Se o dono pedir algo ambíguo, pergunte uma pergunta curta e específica. Não enrole.
- Confirme cada ação com data e hora exatas, sem floreios.
- NUNCA trate o dono como paciente. Não pergunte se ele quer marcar consulta pra ele.

REGRA ABSOLUTA (a mais importante): você SÓ pode dizer que uma ação foi feita
(registrei a venda, bloqueei, agendei, removi) DEPOIS de chamar a ferramenta
correspondente e receber de volta a confirmação com ID. É TERMINANTEMENTE PROIBIDO
dizer "registrei", "anotei", "pronto", "feito" sem ter chamado a ferramenta. Dizer
que registrou uma venda sem chamar registrar_venda faz a venda SUMIR do painel — é o
pior erro possível. Fluxo obrigatório sempre: (1) reúna o necessário → (2) CHAME a
ferramenta → (3) ela retorna sucesso com ID → (4) SÓ ENTÃO confirme ao dono.

VENDA: quando o dono disser que vendeu/fechou, se ele não informou o valor, pergunte
UMA vez ("qual foi o valor?"). Assim que tiver o valor (o número do lead é opcional),
CHAME registrar_venda. Não confirme a venda antes da ferramenta retornar o ID.

USE estas ferramentas:
- bloquear_horario: cria um bloqueio na agenda (período em que a clínica não pode receber agendamentos da Ana com pacientes)
- agendar_paciente_manual: registra um agendamento que veio de fora (telefone direto, walk-in, etc)
- listar_agenda: consulta o que está marcado num período
- listar_bloqueios: consulta bloqueios ativos num período
- remover_bloqueio: remove um bloqueio existente
- verificar_disponibilidade: também disponível, se o dono quiser ver horários livres
- registrar_venda: registra um fecho de negócio. O número do lead é OPCIONAL — peça
  se o cliente falou com você (permite atribuir ao anúncio), mas se a venda veio por
  fora (orgânico, o cliente falou direto com o dono), registre mesmo sem número. O
  valor é importante: pergunte de forma curta se o dono não disser.
"""


FERRAMENTAS_DONO = [
    {
        "name": "bloquear_horario",
        "description": (
            "Cria um bloqueio na agenda. Use quando o dono falar que vai estar "
            "ocupado, vai sair, vai ter cirurgia longa, viagem, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "inicio": {
                    "type": "string",
                    "description": "Início do bloqueio, formato AAAA-MM-DDTHH:MM. Ex: 2026-06-25T14:00"
                },
                "fim": {
                    "type": "string",
                    "description": "Fim do bloqueio, formato AAAA-MM-DDTHH:MM. Ex: 2026-06-25T16:00"
                },
                "motivo": {
                    "type": "string",
                    "description": "Motivo do bloqueio (curto). Ex: 'cirurgia longa', 'compromisso pessoal', 'viagem'."
                }
            },
            "required": ["inicio", "fim"]
        }
    },
    {
        "name": "agendar_paciente_manual",
        "description": (
            "Registra um agendamento que veio por outro canal (telefone, walk-in). "
            "Use quando o dono falar 'marquei X pra tal dia/hora'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "data_hora": {
                    "type": "string",
                    "description": "Data e hora do agendamento, formato AAAA-MM-DDTHH:MM."
                },
                "nome_paciente": {
                    "type": "string",
                    "description": "Nome completo do paciente."
                },
                "telefone_paciente": {
                    "type": "string",
                    "description": "Telefone do paciente (opcional, mas idealmente pedir). Pode ser vazio."
                },
                "observacao": {
                    "type": "string",
                    "description": "Observação opcional do dono."
                }
            },
            "required": ["data_hora", "nome_paciente"]
        }
    },
    {
        "name": "listar_agenda",
        "description": (
            "Consulta agendamentos confirmados num intervalo de datas. "
            "Use quando o dono perguntar 'o que tenho amanhã', 'mostra a semana', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "data_inicio": {
                    "type": "string",
                    "description": "Primeira data do intervalo, formato AAAA-MM-DD."
                },
                "data_fim": {
                    "type": "string",
                    "description": "Última data do intervalo (inclusiva), formato AAAA-MM-DD."
                }
            },
            "required": ["data_inicio", "data_fim"]
        }
    },
    {
        "name": "listar_bloqueios",
        "description": "Consulta bloqueios ativos num intervalo de datas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "data_inicio": {
                    "type": "string",
                    "description": "Primeira data, formato AAAA-MM-DD."
                },
                "data_fim": {
                    "type": "string",
                    "description": "Última data (inclusiva), formato AAAA-MM-DD."
                }
            },
            "required": ["data_inicio", "data_fim"]
        }
    },
    {
        "name": "remover_bloqueio",
        "description": "Remove um bloqueio existente (recebe o ID).",
        "input_schema": {
            "type": "object",
            "properties": {
                "bloqueio_id": {
                    "type": "integer",
                    "description": "ID do bloqueio a remover."
                }
            },
            "required": ["bloqueio_id"]
        }
    },
    # A Ana no modo dono também pode verificar disponibilidade
    {
        "name": "verificar_disponibilidade",
        "description": "Consulta horários livres num intervalo de datas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "data_inicio": {
                    "type": "string",
                    "description": "Primeira data, formato AAAA-MM-DD."
                },
                "data_fim": {
                    "type": "string",
                    "description": "Última data (inclusiva), formato AAAA-MM-DD."
                }
            },
            "required": ["data_inicio", "data_fim"]
        }
    },
    {
        "name": "registrar_venda",
        "description": (
            "Registra uma venda/fecho de negócio. Use quando o dono avisar que FECHOU "
            "com um cliente (ex: 'fechei com o fulano', 'vendi 3 mil'). O número de "
            "WhatsApp do lead é OPCIONAL: se o cliente falou com você em algum momento, "
            "peça o número (permite atribuir a venda ao anúncio na Meta); se a venda "
            "veio por fora (orgânico, o cliente falou direto com o dono), registre "
            "mesmo sem número — a venda conta no faturamento do mesmo jeito. O valor é "
            "importante — pergunte se o dono não disse."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero_lead": {
                    "type": "string",
                    "description": "Número de WhatsApp do lead (com DDD), SE ele falou com você. Ex: 11999998888. Deixe vazio se a venda veio por fora."
                },
                "valor": {
                    "type": "number",
                    "description": "Valor da venda em reais (ex: 3000). Importante, pergunte se não foi dito."
                },
                "descricao": {
                    "type": "string",
                    "description": "Descrição curta do que foi vendido / de quem é (opcional, útil em venda avulsa)."
                }
            },
            "required": []
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

        # Modo multi-profissional: só no contexto de lead (numero_lead preenchido).
        # No modo dono a verificação é da clínica toda (profissional_id=None).
        prof_id = None
        if numero_lead is not None:
            prof_id, erro = _resolver_profissional_lead(args, clinica)
            if erro:
                return erro, extra

        slots = obter_horarios_disponiveis_intervalo(
            clinica["id"], data_ini, data_fim, max_por_dia=4,
            profissional_id=prof_id
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

        observacao = (args.get("observacao") or "").strip() or None

        # Modo multi-profissional: exige profissional_id válido antes de marcar.
        prof_id, erro = _resolver_profissional_lead(args, clinica)
        if erro:
            return erro, extra

        try:
            ag_id = criar_agendamento(
                clinica_id=clinica["id"],
                numero_lead=numero_lead,
                data_hora=dt,
                nome_lead=nome_paciente,
                conversa_id=conversa_id,
                origem="ana",
                observacao=observacao,
                profissional_id=prof_id
            )
            extra["agendamento_criado_id"] = ag_id
            extra["agendamento_data_hora"] = dt
            extra["agendamento_nome"] = nome_paciente
            extra["agendamento_observacao"] = observacao
            extra["agendamento_profissional_id"] = prof_id

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

    elif nome == "consultar_meu_agendamento":
        ags = buscar_agendamentos_ativos_lead(clinica["id"], numero_lead)
        if not ags:
            return "Esse contato não tem nenhum agendamento ativo no momento.", extra

        linhas = []
        for a in ags:
            dh = a["data_hora"]
            motivo = a.get("observacao") or "não informado"
            linhas.append(
                f"  - ID {a['id']}: {_formatar_data_extensa(dh)} às {dh.strftime('%H:%M')} "
                f"| Motivo: {motivo}"
            )
        return "Agendamento(s) ativo(s) desse contato:\n" + "\n".join(linhas), extra

    elif nome == "remarcar_agendamento":
        try:
            ag_id = int(args["agendamento_id"])
            dt = datetime.strptime(args["nova_data_hora"], "%Y-%m-%dT%H:%M")
            dt = dt.replace(tzinfo=timezone(timedelta(hours=-3)))
        except (ValueError, KeyError, TypeError):
            return "Erro: dados inválidos. Confira ID e formato de data (AAAA-MM-DDTHH:MM).", extra

        ag = obter_agendamento(ag_id)
        if not ag:
            return "Erro: agendamento não encontrado.", extra
        if ag["clinica_id"] != clinica["id"] or ag["numero_lead"] != numero_lead:
            return "Erro: esse agendamento não pertence a esse contato.", extra

        try:
            remarcar_agendamento(ag_id, dt)
        except ValueError as e:
            if "ocupado" in str(e):
                return (
                    "Erro: esse novo horário está ocupado. "
                    "Peça desculpa ao lead e proponha outro horário "
                    "(use verificar_disponibilidade de novo)."
                ), extra
            return f"Erro ao remarcar: {e}", extra

        extra["agendamento_criado_id"] = ag_id
        extra["agendamento_data_hora"] = dt
        extra["agendamento_nome"] = ag["nome_lead"]
        extra["agendamento_observacao"] = ag.get("observacao")
        extra["agendamento_tipo"] = "remarcado"

        data_fmt = _formatar_data_extensa(dt)
        hora_fmt = dt.strftime("%H:%M")
        return (
            f"Agendamento remarcado com sucesso pra {data_fmt} às {hora_fmt}. "
            f"Confirme isso ao lead de forma natural na próxima resposta."
        ), extra

    return f"Erro: ferramenta '{nome}' desconhecida.", extra


def _executar_ferramenta_dono(nome, args, clinica):
    """
    Executa as ferramentas do MODO DONO (dentista falando com Ana).
    Retorna (resultado_string, extra_dict).
    """
    extra = {}
    from datetime import timezone, timedelta
    tz = timezone(timedelta(hours=-3))

    if nome == "bloquear_horario":
        try:
            inicio = datetime.strptime(args["inicio"], "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
            fim = datetime.strptime(args["fim"], "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
        except (ValueError, KeyError):
            return "Erro: formato de data/hora inválido. Use AAAA-MM-DDTHH:MM.", extra

        if fim <= inicio:
            return "Erro: o fim do bloqueio precisa ser depois do início.", extra

        motivo = args.get("motivo") or "Bloqueio manual"
        try:
            bid = criar_bloqueio(clinica["id"], inicio, fim, motivo)
            extra["bloqueio_criado_id"] = bid
            return (
                f"Bloqueio #{bid} criado: "
                f"{inicio.strftime('%d/%m %H:%M')} até {fim.strftime('%d/%m %H:%M')}. "
                f"Motivo: {motivo}."
            ), extra
        except Exception as e:
            return f"Erro ao criar bloqueio: {e}", extra

    elif nome == "agendar_paciente_manual":
        try:
            dt = datetime.strptime(args["data_hora"], "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
        except (ValueError, KeyError):
            return "Erro: formato de data/hora inválido.", extra

        nome_paciente = (args.get("nome_paciente") or "").strip()
        if not nome_paciente or len(nome_paciente) < 3:
            return "Erro: nome do paciente é obrigatório.", extra

        telefone = (args.get("telefone_paciente") or "").strip()
        observacao = args.get("observacao") or "Agendado manualmente pelo responsável"

        try:
            ag_id = criar_agendamento(
                clinica_id=clinica["id"],
                numero_lead=telefone or "manual",
                data_hora=dt,
                nome_lead=nome_paciente,
                origem="manual",
                observacao=observacao
            )
            extra["agendamento_criado_id"] = ag_id
            return (
                f"Agendamento #{ag_id} registrado: "
                f"{nome_paciente} em {dt.strftime('%d/%m às %H:%M')}. "
                f"{'Telefone: ' + telefone if telefone else 'Sem telefone.'}"
            ), extra
        except ValueError as e:
            if "ocupado" in str(e):
                return (
                    "Esse horário está ocupado (já tem outro agendamento ou bloqueio). "
                    "Quer ver o que tem ali?"
                ), extra
            return f"Erro: {e}", extra

    elif nome == "listar_agenda":
        try:
            d_ini = datetime.strptime(args["data_inicio"], "%Y-%m-%d").date()
            d_fim = datetime.strptime(args["data_fim"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            return "Erro: formato de data inválido.", extra

        ini_dt = datetime.combine(d_ini, datetime.min.time()).replace(tzinfo=tz)
        fim_dt = datetime.combine(d_fim, datetime.max.time()).replace(tzinfo=tz)
        ags = listar_agendamentos(clinica["id"], ini_dt, fim_dt)

        if not ags:
            return f"Nenhum agendamento entre {d_ini.strftime('%d/%m')} e {d_fim.strftime('%d/%m')}.", extra

        linhas = []
        for a in ags:
            dh = a["data_hora"]
            origem = "Ana" if a["origem"] == "ana" else "Manual"
            linhas.append(
                f"  - {dh.strftime('%d/%m %H:%M')} | {a['nome_lead']} "
                f"({a['numero_lead']}) [{origem}]"
            )
        return f"Agendamentos:\n" + "\n".join(linhas), extra

    elif nome == "listar_bloqueios":
        try:
            d_ini = datetime.strptime(args["data_inicio"], "%Y-%m-%d").date()
            d_fim = datetime.strptime(args["data_fim"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            return "Erro: formato de data inválido.", extra

        ini_dt = datetime.combine(d_ini, datetime.min.time()).replace(tzinfo=tz)
        fim_dt = datetime.combine(d_fim, datetime.max.time()).replace(tzinfo=tz)
        bloqs = listar_bloqueios(clinica["id"], ini_dt, fim_dt)

        if not bloqs:
            return f"Nenhum bloqueio entre {d_ini.strftime('%d/%m')} e {d_fim.strftime('%d/%m')}.", extra

        linhas = []
        for b in bloqs:
            linhas.append(
                f"  - #{b['id']}: {b['inicio'].strftime('%d/%m %H:%M')} → "
                f"{b['fim'].strftime('%d/%m %H:%M')} | {b['motivo'] or 'sem motivo'}"
            )
        return f"Bloqueios:\n" + "\n".join(linhas), extra

    elif nome == "remover_bloqueio":
        try:
            bid = int(args["bloqueio_id"])
        except (ValueError, KeyError, TypeError):
            return "Erro: ID inválido.", extra

        if remover_bloqueio(bid):
            return f"Bloqueio #{bid} removido.", extra
        return f"Bloqueio #{bid} não encontrado.", extra

    elif nome == "registrar_venda":
        # Número é OPCIONAL: se o cliente falou com a agente em algum momento,
        # o número vincula a venda à conversa e permite atribuir ao anúncio.
        # Venda que veio por fora (orgânico, cliente falou direto com o dono) é
        # registrada mesmo assim, como avulsa — conta no faturamento, sem atribuição.
        numero = (args.get("numero_lead") or "").strip()
        conversa = buscar_conversa_por_numero(clinica["id"], numero) if numero else None

        valor = args.get("valor")
        try:
            valor = float(valor) if valor not in (None, "") else None
        except (ValueError, TypeError):
            valor = None
        descricao = (args.get("descricao") or "").strip() or None
        # Se o dono passou um número mas não achamos conversa, guarda no histórico.
        if numero and not conversa and not descricao:
            descricao = f"Venda avulsa (contato informado: {numero})"

        try:
            venda_id = registrar_venda(
                clinica["id"],
                conversa["id"] if conversa else None,
                valor=valor, descricao=descricao
            )
        except Exception as e:
            return f"Erro ao registrar a venda: {e}", extra

        extra["venda_registrada_id"] = venda_id

        # Purchase pra Meta só quando há conversa vinculada com atribuição (ctwa_clid).
        if conversa and clinica.get("capi_ativo"):
            custom = {"value": valor, "currency": "BRL"} if valor is not None else None
            capi.enviar_evento(
                clinica, conversa, "Purchase", f"purchase:{venda_id}",
                custom_data=custom
            )

        valor_txt = ""
        if valor is not None:
            valor_txt = f" no valor de R$ {valor:.2f}".replace(".", ",")
        if conversa:
            destino = f"pro contato {numero}"
        else:
            destino = "(venda avulsa, sem vínculo com conversa)"
        return (
            f"Venda registrada{valor_txt} {destino} (ID {venda_id}). "
            f"Confirme ao dono de forma curta e direta."
        ), extra

    elif nome == "verificar_disponibilidade":
        # Reutiliza a mesma lógica da Ana com lead
        return _executar_ferramenta("verificar_disponibilidade", args, clinica, None, None)

    return f"Ferramenta '{nome}' desconhecida.", extra



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
                     clinica=None, conversa_id=None, numero_lead=None,
                     modo_dono=False):
    """
    Chama a API do Claude e retorna o texto da resposta.
    Agora suporta tool use: a Ana pode chamar ferramentas de agendamento.

    Se modo_dono=True, usa prompt e ferramentas de "assistente executiva do dono".

    Retorna (texto_resposta, lista_de_agendamentos_criados, meta), onde meta é um
    dict com sinais pro rastreamento (ex: consultou_disponibilidade → evento Lead).
    """
    messages = list(historico)
    messages.append({"role": "user", "content": mensagem_atual})
    meta = {"consultou_disponibilidade": False}

    if modo_dono:
        # Modo dono: prompt fixo de assistente executiva + ferramentas administrativas
        system_completo = PROMPT_MODO_DONO + construir_contexto_temporal()
        if clinica:
            system_completo += _formatar_config_horarios_pro_prompt(clinica["id"])
        ferramentas_disponiveis = FERRAMENTAS_DONO
        executar_func = lambda nome, args: _executar_ferramenta_dono(nome, args, clinica)
    else:
        # Modo lead: prompt personalizado da clínica + ferramentas de agendamento
        system_completo = system_prompt + construir_contexto_temporal()
        ferramentas_disponiveis = FERRAMENTAS
        if clinica:
            system_completo += _formatar_config_horarios_pro_prompt(clinica["id"])
            # Multi-profissional: injeta a lista de profissionais e ativa o
            # profissional_id nas ferramentas. Clínica sem profissionais = fluxo antigo.
            profissionais = listar_profissionais(clinica["id"])
            if profissionais:
                system_completo += _formatar_profissionais_pro_prompt(profissionais)
            system_completo += INSTRUCOES_AGENDAMENTO
            ferramentas_disponiveis = _ferramentas_lead(bool(profissionais))
            # Nome já registrado deste contato (robusto à janela de histórico):
            # evita a Ana perguntar o nome de novo ao remarcar/retornar.
            if numero_lead:
                nome_conhecido = obter_nome_lead(clinica["id"], numero_lead)
                if nome_conhecido:
                    system_completo += (
                        "\n\n===== PACIENTE JÁ IDENTIFICADO =====\n"
                        f"O nome completo deste contato já está registrado: {nome_conhecido}.\n"
                        f"NÃO pergunte o nome de novo — use '{nome_conhecido}' ao criar ou "
                        "remarcar agendamento. Só peça o nome se ficar claro que é outra "
                        "pessoa (ex: marcando pra um familiar).\n"
                        "================================================================="
                    )
        executar_func = lambda nome, args: _executar_ferramenta(
            nome, args, clinica, conversa_id, numero_lead
        )

    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    agendamentos_criados = []
    for tentativa in range(5):
        payload = {
            "model": CLAUDE_MODEL,
            "max_tokens": 1024,
            "temperature": 0.7,
            "system": system_completo,
            "messages": messages,
        }
        if clinica:
            payload["tools"] = ferramentas_disponiveis

        try:
            r = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=45)
            if r.status_code != 200:
                print(f"❌ Claude retornou {r.status_code}: {r.text}")
                return None, agendamentos_criados, meta
            data = r.json()
        except Exception as e:
            print(f"❌ Erro ao chamar Claude: {e}")
            return None, agendamentos_criados, meta

        stop_reason = data.get("stop_reason")
        content = data.get("content", [])

        if stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": content})

            tool_results = []
            for bloco in content:
                if bloco.get("type") != "tool_use":
                    continue
                nome_tool = bloco["name"]
                tool_id = bloco["id"]
                args = bloco.get("input", {})
                modo_label = "DONO" if modo_dono else "LEAD"
                print(f"🔧 [{modo_label}] Ana chamou: {nome_tool}({args})")
                # Sinal de qualificação (evento Lead): lead pediu pra ver horários.
                if not modo_dono and nome_tool == "verificar_disponibilidade":
                    meta["consultou_disponibilidade"] = True
                resultado, extra = executar_func(nome_tool, args)
                print(f"   → {resultado[:120]}")
                if "agendamento_criado_id" in extra:
                    agendamentos_criados.append(extra)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": resultado,
                })

            messages.append({"role": "user", "content": tool_results})
            continue

        textos = [b["text"] for b in content if b.get("type") == "text"]
        return ("\n".join(textos).strip(), agendamentos_criados, meta)

    print("⚠️ Loop de tool use excedeu 5 tentativas.")
    return None, agendamentos_criados, meta


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
def _remover_travessao(texto):
    """
    Substitui travessão longo (—) por vírgula. Os prompts já instruem a Ana
    a não usar, mas o modelo insiste às vezes — isso garante 100% independente
    do prompt de cada cliente.
    """
    return texto.replace(" — ", ", ").replace("—", ", ")


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

    Detecta se a mensagem é do DONO da clínica (telefone_humano) e usa
    o modo apropriado (assistente executiva) em vez do modo lead.
    """
    try:
        print(f"🟢 [BG] Iniciando processamento conversa={conversa_id} numero={numero_lead}")
        # Token específico da clínica (ou None = usa o global como fallback).
        token_clinica = clinica.get("whatsapp_token")

        # MODO DONO: verifica se o número que enviou é o telefone_humano dessa clínica.
        # Comparação robusta (ignora DDI e o 9º dígito, que variam de formato).
        modo_dono = False
        tel_humano = clinica.get("telefone_humano")
        if tel_humano:
            can_humano = _telefone_canonico(tel_humano)
            can_lead = _telefone_canonico(numero_lead)
            if can_humano and can_lead and can_humano == can_lead:
                modo_dono = True
                print(f"🔑 [{clinica['nome']}] MODO DONO ativado pra {numero_lead}")
            else:
                print(f"🙅 [{clinica['nome']}] não é dono: lead={numero_lead} "
                      f"(canon={can_lead}) vs humano='{tel_humano}' (canon={can_humano})")

        print(f"🟢 [BG] modo_dono={modo_dono}, vai marcar como lida...")
        # 1) Mostra check azul + "digitando..." imediatamente
        marcar_como_lida_e_digitando(
            clinica["phone_number_id"], message_id_whatsapp,
            token=token_clinica
        )
        print(f"🟢 [BG] check azul OK")

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

        # 2) Salva a mensagem (sempre, mesmo modo dono).
        print(f"🟢 [BG] Salvando mensagem no banco...")
        salvar_mensagem(
            conversa_id, "user", texto_recebido,
            message_id_whatsapp=message_id_whatsapp
        )
        print(f"🟢 [BG] Mensagem salva")

        # 2.5) Se humano assumiu a conversa, Ana fica calada.
        # Mas isso NÃO se aplica no modo dono (dono falando direto sempre tem prioridade).
        if not modo_dono and conversa_esta_pausada(conversa_id):
            print(f"⏸️  Conversa {conversa_id} pausada (humano assumiu) — Ana em silêncio.")
            return

        # 3) Carrega o histórico recente da conversa.
        print(f"🟢 [BG] Carregando histórico...")
        historico = obter_historico_conversa(conversa_id, limite=20)
        if historico and historico[-1]["role"] == "user":
            historico_base = historico[:-1]
        else:
            historico_base = historico
        print(f"🟢 [BG] Histórico carregado: {len(historico_base)} mensagens. Chamando Claude...")

        # 4) Gera resposta. Passa flag modo_dono.
        resposta_completa, agendamentos, meta_ia = gerar_resposta_ia(
            clinica["system_prompt"], historico_base, texto_recebido,
            clinica=clinica, conversa_id=conversa_id, numero_lead=numero_lead,
            modo_dono=modo_dono
        )
        print(f"🟢 [BG] Claude respondeu. Tamanho: {len(resposta_completa) if resposta_completa else 0}")

        if not resposta_completa:
            telefone_humano = clinica.get("telefone_humano") or ""
            fallback = "Desculpe, tive um problema técnico. Pode tentar novamente"
            fallback += f" ou ligar no {telefone_humano}?" if telefone_humano else "?"
            enviar_mensagem_whatsapp(
                clinica["phone_number_id"], numero_lead, fallback,
                token=token_clinica
            )
            return

        resposta_completa = _remover_travessao(resposta_completa)

        # 5) Salva a resposta da Ana inteira no banco.
        salvar_mensagem(conversa_id, "assistant", resposta_completa)

        # 6) Quebra em partes e envia cada uma com delay natural.
        partes = quebrar_em_partes(resposta_completa)
        for parte in partes:
            time.sleep(calcular_delay(parte))
            enviar_mensagem_whatsapp(
                clinica["phone_number_id"], numero_lead, parte,
                token=token_clinica
            )

        modo_label = "DONO" if modo_dono else "LEAD"
        print(f"💬 [{clinica['nome']}] [{modo_label}] Ana respondeu em {len(partes)} parte(s) pra {numero_lead}")

        # 7) Se Ana agendou no modo lead, notifica o dono.
        # No modo dono, NÃO notifica (o próprio dono fez a ação).
        if not modo_dono:
            for ag in agendamentos:
                notificar_agendamento_para_responsavel(clinica, ag, numero_lead)

        # 8) Rastreamento de conversões (CAPI). Só toca no banco/rede se o tenant
        # tem CAPI ativo e é conversa de lead. enviar_evento é no-op sem ctwa_clid
        # e faz dedup — pode ser chamado à vontade, nunca quebra o atendimento.
        #
        # A Conversions API de MENSAGENS (WhatsApp) só aceita dois nomes de evento:
        # "LeadSubmitted" e "Purchase". "Contact"/"Lead"/"Schedule" são recusados (400).
        # Por isso: agendamento marcado = LeadSubmitted (lead qualificado de verdade);
        # a venda vira Purchase (disparado no painel / modo dono).
        if not modo_dono and clinica.get("capi_ativo"):
            conversa = obter_conversa(conversa_id)
            for ag in agendamentos:
                ag_id = ag.get("agendamento_criado_id")
                if ag_id:
                    capi.enviar_evento(clinica, conversa, "LeadSubmitted",
                                       f"leadsubmitted:{ag_id}")

    except Exception as e:
        import traceback
        print(f"❌ Erro no processamento de background: {e}")
        print("🔍 Stack trace completo:")
        traceback.print_exc()


# ============================================================
# FOLLOW-UP (agendador proativo em background)
# ============================================================
FOLLOWUP_OPTOUT_FRASES = [
    "não quero mais", "nao quero mais", "parar de receber", "pare de receber",
    "pare de me mandar", "para de me mandar", "sair da lista", "me descadastr",
    "não me mande", "nao me mande", "não me manda", "nao me manda",
    "quero sair", "cancelar inscri", "descadastrar",
]


def _texto_pede_optout(texto):
    """Detecta pedido explícito de parar de receber follow-up (conservador)."""
    t = (texto or "").lower()
    return any(frase in t for frase in FOLLOWUP_OPTOUT_FRASES)


def _enviar_lembretes():
    """Dispara os lembretes de consulta que estão na hora (mesmo dia)."""
    agora = _agora_brasil()
    for ag in listar_agendamentos_lembrete(agora):
        # Trava: só um worker/ciclo envia este lembrete.
        if not followup_claim(ag["clinica_id"], "lembrete", "agendamento", ag["id"], 1):
            continue
        try:
            nome = (ag.get("nome_lead") or "").strip().split(" ")[0] or "Olá"
            data_fmt = _formatar_data_lembrete(ag["data_hora"])
            hora_fmt = ag["data_hora"].strftime("%H:%M")
            ok = enviar_template_whatsapp(
                ag["phone_number_id"], ag["numero_lead"],
                ag["followup_template_lembrete"],
                [nome, data_fmt, hora_fmt],
                token=ag.get("whatsapp_token"),
            )
            followup_resultado("lembrete", "agendamento", ag["id"], 1,
                               "enviado" if ok else "erro")
            print(f"⏰ Lembrete {'enviado' if ok else 'FALHOU'} ag#{ag['id']} "
                  f"({ag['clinica_nome']}) pra {ag['numero_lead']}")
        except Exception as e:
            followup_resultado("lembrete", "agendamento", ag["id"], 1, "erro", str(e))
            print(f"❌ Erro no lembrete ag#{ag['id']}: {e}")


def _enviar_frio(conv, tentativa):
    try:
        ok = enviar_template_whatsapp(
            conv["phone_number_id"], conv["numero_lead"],
            conv["followup_template_frio"], [],  # template genérico, sem variáveis
            token=conv.get("whatsapp_token"),
        )
        followup_resultado("frio", "conversa", conv["id"], tentativa,
                           "enviado" if ok else "erro")
        print(f"🔁 Follow-up frio t{tentativa} {'enviado' if ok else 'FALHOU'} "
              f"conversa#{conv['id']} ({conv['clinica_nome']}) pra {conv['numero_lead']}")
    except Exception as e:
        followup_resultado("frio", "conversa", conv["id"], tentativa, "erro", str(e))
        print(f"❌ Erro no follow-up frio conversa#{conv['id']}: {e}")


def _enviar_frios():
    """Reativa leads frios (24h e 72h), só em horário civilizado (8h–20h)."""
    if not (8 <= _agora_brasil().hour < 20):
        return
    for conv in listar_conversas_frio_t1():
        if not followup_claim(conv["clinica_id"], "frio", "conversa", conv["id"], 1):
            continue
        _enviar_frio(conv, 1)
    for conv in listar_conversas_frio_t2():
        if not followup_claim(conv["clinica_id"], "frio", "conversa", conv["id"], 2):
            continue
        _enviar_frio(conv, 2)


def _loop_followup():
    """Acorda a cada 10 min e processa os follow-ups pendentes."""
    time.sleep(45)  # deixa o app terminar de subir antes do 1º ciclo
    while True:
        try:
            _enviar_lembretes()
            _enviar_frios()
        except Exception as e:
            print(f"❌ Erro no loop de follow-up: {e}")
        time.sleep(600)


def iniciar_agendador_followup():
    threading.Thread(target=_loop_followup, daemon=True).start()
    print("✅ Agendador de follow-up iniciado.")


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

                    # Atribuição de anúncio (click-to-WhatsApp): vem junto da
                    # mensagem quando o lead chegou por um anúncio. Capturamos SEMPRE.
                    referral = message.get("referral")

                    if msg_type == "text":
                        texto = message.get("text", {}).get("body", "")
                        print(f"\n📨 [{clinica['nome']}] {sender}: {texto}")

                        conversa_id = obter_ou_criar_conversa(
                            clinica["id"], sender
                        )
                        if referral:
                            salvar_referral_conversa(conversa_id, referral)
                            print(f"🎯 Referral capturado (ctwa_clid={referral.get('ctwa_clid')})")
                        # Opt-out de follow-up: lead pediu pra parar de receber.
                        if _texto_pede_optout(texto):
                            marcar_optout_conversa(conversa_id)
                            print(f"🚫 Opt-out de follow-up marcado pra {sender}")

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
                        if referral:
                            salvar_referral_conversa(conversa_id, referral)
                            print(f"🎯 Referral capturado (ctwa_clid={referral.get('ctwa_clid')})")

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

# Agendador de follow-up (inerte enquanto nenhum cliente tiver follow-up ativo).
try:
    iniciar_agendador_followup()
except Exception as e:
    print(f"⚠️  Não foi possível iniciar o agendador de follow-up: {e}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

"""Prompts.

The PLUGIR_* prompts are copied verbatim from PlugIR's generate_dialog.py
(github.com/Saehyung-Lee/PlugIR @ ce79c9f) so the baseline is faithful. Only the
message plumbing is ours. Prompts we add for our own methods are marked AGENTIR_*.
"""
from __future__ import annotations

from .state import Turn


def _qa_lines(turns: list[Turn]) -> str:
    return "".join(f"Question: {t.question}\nAnswer: {t.answer}\n" for t in turns)


# ----------------------------------------------------------------------------- reconstruct
_RECON_EXAMPLE_DIALOG = ", ".join([
    "is this in a park? yes, i believe it is", "are there others around? no, she is alone",
    "does she have a collection bucket? no", "is her hair long? yes, pretty long",
    "is she wearing a dress? i don't think so, hard to tell", "does she have shoes on? yes, flip flops",
    "is there grass nearby? yes, everywhere", "is it a sunny day? yes",
    "are there trees? in the background there are trees", "is the guitar new? i don't think so"])


def plugir_reconstruct(caption: str, turns: list[Turn]) -> list[dict]:
    dialog = ", ".join(t.as_text() for t in turns)
    return [
        {"role": "system", "content": "Your role is to reconstruct the [Caption] with the additional information given by following [Dialogue]. The reconstructed [New Caption] should be concise and in appropriate form to retrieve a target image from a pool of candidate images"},
        {"role": "user", "content": f"[Caption]: a woman sits on a bench holding a guitar in her lap [Dialogue]: {_RECON_EXAMPLE_DIALOG}  [New Caption]: "},
        {"role": "assistant", "content": "a woman with pretty long hair sits alone on a grassy bench in a park on a sunny day, holding a guitar in her lap without a collection bucket, wearing flip flops, with trees in the background, with a slightly worn guitar"},
        {"role": "user", "content": f"[Caption]: {caption} [Dialogue]: {dialog}  [New Caption]: "},
    ]


def plugir_paraphrase(text: str) -> list[dict]:
    return [
        {"role": "system", "content": "Your role is to paraphrase the given text into a fluent and natural text while preserving the information in the given text. Do not add your internal knowledge while paraphrasing."},
        {"role": "user", "content": f"Text: {text}\nParaphrased: "},
    ]


# ----------------------------------------------------------------------------- questions
def plugir_generate_plain(caption: str, turns: list[Turn]) -> list[dict]:
    sys = ("You are a proficient question generator tasked with aiding in the retrieval of a target image. "
           "Your role is to generate questions about the target image of the description via "
           "leveraging two key information sources:\n\n"
           "[Description]: This is a concise explanation of the target image.\n"
           "[Dialogue]: Comprising question and answer pairs that seek additional "
           "details about the target image.\n"
           "Your generated question about the description must be clear, succinct, and concise, "
           "while differing from prior questions in the [Dialogue].")
    example = ("[Description]\n"
               "a man is doing a trick on a skateboard\n"
               "\n[Dialogue]\n"
               "Question: What type of trick is the man performing on the skateboard?\n"
               "Answer: a jump\n"
               "Question: What is the location of the jump trick being performed?\n"
               "Answer: a skate park\n"
               "Question: ")
    user = "\n[Description]\n" + caption + "\n" + "\n[Dialogue]\n" + _qa_lines(turns) + "Question: "
    return [{"role": "system", "content": sys}, {"role": "user", "content": example},
            {"role": "assistant", "content": "what is the outfit of the man performing the jump trick at a skate park?"},
            {"role": "user", "content": user}]


def plugir_generate_referring(caption: str, turns: list[Turn], candidates_text: str,
                              prior_questions: list[str]) -> list[dict]:
    sys = ("You are a proficient question generator tasked with aiding in the retrieval of a target image. "
           "Your role is to generate questions about the target image of the description via "
           "leveraging three key information sources:\n\n"
           "[Retrieval Candidates]: These are captions of images which are the candidates "
           "of the retrieval task for the target image described in [Description].\n"
           "[Description]: This is a concise explanation of the target image.\n"
           "[Dialogue]: Comprising question and answer pairs that seek additional "
           "details about the target image.\n\n"
           "You should craft a question that narrows down the options for "
           "the attributes of the target image through "
           "drawing the information from the retrieval candidates. "
           "The generated question about the target image must be clear, succinct, and concise. "
           "Also, the question should only be asked about common objects in the description and candidates, "
           "which cannot be answered only from the description and the dialogue. "
           "Please explain how did you utilize the information sources for generating a question.\n")
    example = ("[Retrieval Candidates]\n"
               "0. man in yellow shirt\n"
               "1. a boy in a skateboard park\n"
               "2. the biker is performing a trick\n"
               "3. a man in a green hat doing half-pipe with a skateboard\n"
               "4. a skateboarding man catches the air in the midst of a trick\n"
               "[Description]\n"
               "a man is doing a trick on a skateboard\n"
               "[Dialogue]\n"
               "Question: what type of trick is the man performing on the skateboard?\n"
               "Answer: a jump\n"
               "Question: what is the location of the jump trick being performed?\n"
               "Answer: a skate park\n"
               "Question: ")
    assistant = ("what is the outfit of the man performing the jump trick at a skate park?\n"
                 "Explanation:To generate a question about the description, I will utilize the "
                 "retrieval candidates that mention the outfit of the man. Candidates 0 and 3 "
                 "provide information about the man's wearing. "
                 "The description mentions the man's trick on a skateboard, and the dialogue mentions "
                 "the type and the location of the trick. "
                 "Since the attribute about the outfit is not appeared at the description and the dialogue, "
                 "the generated question cannot be answered from the information of "
                 "the description and the dialogue about the target image. "
                 "Also, the generated question is asking for the common objective, man, in the"
                 " descriptions and candidates, "
                 "not for the different objective from the description and the retrieval candidates 0 and 3, "
                 "for example, a shirt and a half-pipe.")
    # NB: PlugIR omits the newline after the description here; kept for faithfulness.
    user = ("[Retrieval Candidates]\n" + candidates_text + "[Description]\n" + caption + "[Dialogue]\n"
            + _qa_lines(turns) + "".join(f"Question: {q}\nAnswer:\n" for q in prior_questions) + "Question: ")
    return [{"role": "system", "content": sys}, {"role": "user", "content": example},
            {"role": "assistant", "content": assistant}, {"role": "user", "content": user}]


def plugir_filter(context: str, question: str) -> list[dict]:
    sys = ("Answer the question only according to the given context. "
           "If you cannot determine the answer or there are no objects "
           "that are asked by the question in the context , answer \"Uncertain\".")
    user = "[Context]\n" + context + "\n[Question]\n" + question + "\n[Answer]\n"
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def first_question(text: str) -> str:
    """PlugIR keeps everything up to the first '?' of the completion."""
    return text.split("?")[0].strip() + "?"


# ----------------------------------------------------------------------------- ours
def agentir_propose_answers(caption: str, turns: list[Turn], question: str, num_answers: int,
                            candidate_captions: list[str] | None = None) -> list[dict]:
    sys = ("You help an image-search assistant decide which question to ask a user. The user is "
           "looking for one specific image they have in mind. Given what is known so far and a "
           "candidate question, list the different short answers the user could plausibly give. "
           "Answers must be consistent with the known information, mutually distinct, and phrased "
           "the way a person would answer in a chat (a few words). Prefer answers that correspond to "
           "genuinely different images rather than synonyms. "
           f"Reply with only a JSON array of exactly {num_answers} strings.")
    parts = []
    if candidate_captions:
        parts.append("[Images currently in the collection that match the description]\n"
                     + "".join(f"{i}. {c}\n" for i, c in enumerate(candidate_captions)))
    parts.append("[Description]\n" + caption + "\n")
    parts.append("[Dialogue]\n" + (_qa_lines(turns) or "(none yet)\n"))
    parts.append("[Candidate question]\n" + question + "\n")
    parts.append(f"[{num_answers} plausible answers as a JSON array]\n")
    return [{"role": "system", "content": sys}, {"role": "user", "content": "".join(parts)}]


def user_sim_messages(question: str, caption: str | None) -> tuple[str, str]:
    """(system, user-text) for a VLM playing the user who has the target image in mind.

    `caption` is the current retrieval query (PlugIR passes `caption_recon` to BLIP-2).
    """
    sys = ("You are a person searching for the photo shown to you. A search assistant asks you "
           "questions about it. Answer truthfully based only on what is visible in the photo, "
           "briefly and casually, in a few words, the way a person would reply in a chat. If the "
           "photo does not show enough to answer, say you can't tell. Never describe the photo "
           "beyond what the question asks.")
    text = (f"(So far you have described the photo as: \"{caption}\")\n" if caption else "") + f"Question: {question}"
    return sys, text

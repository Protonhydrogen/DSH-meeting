"""Conference-specific spelling hints, not claims about a speaker's results."""
from pathlib import Path
import json

CATALOG = Path(__file__).resolve().parents[2] / 'config' / 'chemistry-biology-glossary.json'

def catalog():
    general = json.loads(CATALOG.read_text(encoding='utf-8-sig'))
    return {'general':general,'term_definitions':general['term_definitions']}


def profile(profile_id='general'):
    if profile_id != 'general':
        raise ValueError('Unknown vocabulary profile')
    general = catalog()['general']
    terms = general['core'] + [term for values in general['topics'].values() for term in values]
    return {'id':'general','topic':'General chemistry and life sciences','terms':list(dict.fromkeys(terms))}

def prompt(profile_id='general'):
    p = profile(profile_id)
    # Short hints only: do not crowd the decoder context with the whole agenda.
    result = 'English scientific speech. Vocabulary: '
    for term in p['terms'][:24]:
        if len(result) + len(term) + 2 > 700:
            break
        result += term + ', '
    return result.rstrip(', ')


def contextual_terms(text, limit=80):
    """Rank domain topics without using author names or speaker labels."""
    data=catalog()['general']
    text=text.lower()
    ranked=sorted(data['topics'].values(),key=lambda terms:sum(1 for t in terms if len(t)>3 and t.lower() in text),reverse=True)
    return list(dict.fromkeys(data['core']+[t for terms in ranked[:2] for t in terms]))[:limit]

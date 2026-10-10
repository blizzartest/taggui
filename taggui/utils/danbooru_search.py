import re


class DanbooruSearchError(ValueError):
    pass


def _tokenize(text: str) -> list:
    tokens = []
    i = 0
    text_length = len(text)
    while i < text_length:
        char = text[i]
        if char.isspace():
            i += 1
            continue
        if char == ')':
            tokens.append(')')
            i += 1
            continue
        prefix = ''
        while i < text_length and text[i] in '-~':
            prefix += text[i]
            i += 1
        if i < text_length and text[i] == '(':
            tokens.append(('group', prefix))
            i += 1
            continue
        if i < text_length and text[i] == '"':
            quote_end = text.find('"', i + 1)
            if quote_end == -1:
                raise DanbooruSearchError('Unclosed quote')
            word = text[i + 1:quote_end]
            i = quote_end + 1
        else:
            start = i
            while (i < text_length and not text[i].isspace()
                   and text[i] not in '()'):
                i += 1
            word = text[start:i]
            if not word:
                raise DanbooruSearchError('Incomplete search term')
        tokens.append(('term', prefix, word))
    return tokens


def _apply_prefix(prefix: str, filter_):
    for char in prefix:
        if char == '-':
            filter_ = ['NOT', filter_]
    is_disjunctive = '~' in prefix
    return filter_, is_disjunctive


def _fold(operands: list, operator: str):
    result = operands[0]
    for operand in operands[1:]:
        result = [result, operator, operand]
    return result


class _Parser:
    def __init__(self, tokens: list):
        self.tokens = tokens
        self.pos = 0

    def _peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def parse(self):
        if not self.tokens:
            raise DanbooruSearchError('Empty search')
        result = self.parse_or()
        if self.pos != len(self.tokens):
            raise DanbooruSearchError("Unexpected ')'")
        return result

    def parse_or(self) -> list:
        operands = [self.parse_and()]
        while True:
            token = self._peek()
            if (isinstance(token, tuple) and token[0] == 'term'
                    and token[1] == '' and token[2].lower() == 'or'):
                self.pos += 1
                operands.append(self.parse_and())
            else:
                break
        if len(operands) == 1:
            return operands[0]
        return _fold(operands, 'OR')

    def parse_and(self) -> list:
        operands = []
        disjunctive_operands = []
        while True:
            token = self._peek()
            if token is None or token == ')':
                break
            if (isinstance(token, tuple) and token[0] == 'term'
                    and token[1] == '' and token[2].lower() == 'or'):
                break
            filter_, is_disjunctive = self.parse_factor()
            if is_disjunctive:
                disjunctive_operands.append(filter_)
                continue
            if disjunctive_operands:
                operands.append(_fold(disjunctive_operands, 'OR'))
                disjunctive_operands = []
            operands.append(filter_)
        if disjunctive_operands:
            operands.append(_fold(disjunctive_operands, 'OR'))
        if not operands:
            raise DanbooruSearchError('Incomplete search')
        if len(operands) == 1:
            return operands[0]
        return _fold(operands, 'AND')

    def parse_factor(self):
        token = self._peek()
        if token is None or token == ')':
            raise DanbooruSearchError('Incomplete search')
        self.pos += 1
        if not isinstance(token, tuple):
            raise DanbooruSearchError('Incomplete search')
        if token[0] == 'group':
            inner = self.parse_or()
            if self._peek() != ')':
                raise DanbooruSearchError("Missing ')'")
            self.pos += 1
            return _apply_prefix(token[1], inner)
        return _apply_prefix(token[1], ['tag', token[2]])


def parse_danbooru_search_text(text: str) -> list | None:
    """Parse Danbooru booru search syntax into a taggui filter expression."""
    if not text.strip():
        return None
    return _Parser(_tokenize(text)).parse()


def quote_tag_for_danbooru_search(tag: str) -> str:
    """Quote a tag if it contains characters that are special in the Danbooru
    search syntax."""
    if re.search(r'[\s()]', tag):
        return f'"{tag}"'
    return tag

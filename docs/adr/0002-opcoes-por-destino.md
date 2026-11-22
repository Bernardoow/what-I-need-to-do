# Gerar opções de CLI a partir das colunas do destino

Todo destino oferece título e estado, e os nomes das colunas correspondentes podem variar entre bases. Ao registrar uma base, o CLI identifica as colunas opcionais que o adaptador suporta e gera opções de comando a partir dos nomes delas, como `--tag`. Isso mantém os mesmos comandos de tarefa em todos os destinos e permite usar campos específicos da base ativa.

## Consequências

- No Notion, o título é identificado pelo tipo da coluna. No cadastro do destino, o usuário escolhe a coluna do tipo Status e os valores que indicam uma tarefa pendente ou concluída.
- Os nomes das opções usam minúsculas, sem acentos e com hífens entre palavras. `win-td update --help` mostra as opções do destino ativo.
- Na primeira versão, as opções geradas a partir de colunas opcionais cobrem o tipo de seleção múltipla.
- Em atualizações, passar uma tag acrescenta o rótulo à tarefa e mantém as tags existentes. O CLI pede confirmação antes de criar uma opção nova na base.
- Cada destino pode mapear colunas de data e hora para o início e a conclusão. `win-td update ID --start-now` registra o início, e `win-td done ID` registra a conclusão quando a coluna estiver configurada. Repetir o comando preserva o horário já registrado.

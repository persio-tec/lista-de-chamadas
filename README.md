# Lista de Chamadas

Aplicativo local para cadastro de pessoas, chamada diária e exportação da lista em CSV. A identificação usa os modelos faciais YuNet e SFace do OpenCV e é processada neste computador.

## Abrir

Use **Lista de Chamadas** no menu de aplicativos do Linux ou execute:

```sh
python3 /home/persio/Projetos/lista-de-chamadas/launch.py
```

Ao abrir o aplicativo, a câmera inicia automaticamente e registra cada passagem de uma pessoa cadastrada. Permanecer diante da câmera não cria registros repetidos; uma nova passagem é contada depois que o rosto deixa de ser reconhecido por alguns quadros e volta a aparecer. Para cadastrar uma pessoa, informe o nome, confirme o consentimento dela e deixe apenas um rosto visível na câmera. Se alguém da lista aparecer como não cadastrado, use **Atualizar rosto** na linha da pessoa, confirme o consentimento e deixe somente o rosto dessa pessoa diante da câmera durante a captura das cinco amostras. Use **Pausar chamada** para desligar a câmera; a chamada também pode ser feita manualmente.

A seção **Histórico de presença** mostra os registros individuais com data, hora e tipo (reconhecimento facial ou manual). Por padrão, exibe todos os dias; use o filtro de data para consultar um dia específico e **Exportar histórico** para salvar os resultados em CSV. A exportação da lista diária continua disponível separadamente. Em caso de falha ao abrir a câmera, feche outros programas que possam estar usando-a e tente novamente.

## Dados e privacidade

- Os dados da lista e os vetores faciais ficam em `~/.local/share/lista-de-chamadas/chamadas.sqlite3`.
- As imagens da câmera não são armazenadas nem transmitidas; a câmera só é iniciada enquanto a aplicação está aberta.
- O processamento e o servidor HTTP ficam limitados a `127.0.0.1`.
- O servidor local continua disponível em segundo plano até o computador ser desligado; a câmera é desligada quando a janela do aplicativo é fechada.
- O cadastro facial exige consentimento. Remover uma pessoa exclui o cadastro facial e o histórico de presença associado.
- O cadastro mantém cinco amostras faciais locais para melhorar o reconhecimento sem reduzir o limite de comparação.
- Cada passagem reconhecida é armazenada no histórico local; os registros diários anteriores são migrados para o histórico na primeira inicialização da nova versão.
- A comparação facial pode falhar ou identificar incorretamente. Revise os registros e use a marcação manual quando necessário. O reconhecimento não comprova presença física nem detecta tentativas de fraude.

## Modelos

Modelos OpenCV Zoo:

- YuNet (`face_detection_yunet_2023mar.onnx`), OpenCV Zoo: <https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet>
- SFace (`face_recognition_sface_2021dec.onnx`), OpenCV Zoo: <https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface>

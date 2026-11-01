"""Download public ONNX weights once. No user task or experience is uploaded."""

from repo_agent.memory_retrieval import LocalEmbedding

if __name__ == "__main__":
    from fastembed import TextEmbedding

    config = LocalEmbedding()
    TextEmbedding(config.model_name, cache_dir=str(config.cache), threads=2)
    print("Local memory model prepared; runtime uses local_files_only=True.")

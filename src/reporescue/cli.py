import argparse

from .models import BugTask


def main():
    parser = argparse.ArgumentParser(description="RepoRescue workflow scaffold")
    parser.add_argument("--title", default="Checkout returns HTTP 500")
    parser.add_argument(
        "--description", default="Checkout fails after recent deployment."
    )
    parser.add_argument("--reporter", default="demo-user")
    args = parser.parse_args()
    task = BugTask(args.title, args.description, args.reporter)
    print(f"Created task {task.task_id}: {task.title} [{task.status}]")


if __name__ == "__main__":
    main()
